# RBAC Design — Corporate Bot Permission System

## Overview

Role-Based Access Control for the corporate AI assistant. Unifies the current
fragmented permission checks (7+ enforcement layers) into a coherent model.

## Core Primitives

### Subjects (WHO can act)

| Subject Type | Identifier | How Resolved | Example |
|-------------|-----------|-------------|---------|
| **User** | Azure AD Object ID | JWT token from Teams webhook | `alice-aad-123` |
| **Team** | Azure AD Group ID / Teams team ID | Graph API group membership | `engineering-team-456` |
| **Chat** | Conversation ID (Teams/TG/WA) | Message origin | `teams:19:abc@thread.tacv2` |

**Inheritance:**
- A User inherits privileges from all Teams they belong to
- A Chat inherits privileges from its assigned Harness
- A User in a Chat gets: `union(user_privileges, user_team_privileges, chat_privileges)`
- **Most restrictive wins** for conflicting policies (deny overrides allow)

### Privileges (WHAT actions can be performed)

| Privilege | Meaning | Applies To |
|-----------|---------|-----------|
| `admin` | Full control — create, modify, delete, configure | All objects |
| `read` | View, search, retrieve | Data objects (domains, facts, chat history) |
| `write` | Create, modify | Data objects (facts, documents, domain content) |
| `use` | Invoke, execute | Tools, capabilities, integrations |
| `execute` | Run code, system commands | Filesystem, network, sandbox |

### Objects (WHAT is being accessed)

| Object Type | Scope | Examples |
|------------|-------|---------|
| **Knowledge Domain** | Domain ID | `eng-runbooks`, `hr-policies` |
| **Credentials** | Credential key / scope | OAuth tokens, API keys, shared secrets |
| **Tools** | MCP tool name/prefix | `mcp__bot-messaging__telegram_send_message` |
| **Chat Data** | Chat ID | Messages, media, chat-scoped facts |
| **Domain Data** | Domain ID | Domain-scoped facts, document chunks |
| **Filesystem** | Path scope | Sandbox dir (per chat), full `/host-repo/` (admin) |
| **Network** | Access level | Sandboxed (TCP 80/443 proxy), full, none |

## Policy Model

### Policy Structure

```yaml
policy:
  id: "eng-team-domain-access"
  subject:
    type: team                    # user | team | chat
    id: "engineering-team-456"    # or pattern: "Engineering*"
  privilege: read                 # admin | read | write | use | execute
  object:
    type: knowledge_domain        # knowledge_domain | credentials | tools | chat_data | domain_data | filesystem | network
    id: "eng-runbooks"           # specific ID, or "*" for all
  effect: allow                   # allow | deny
  conditions:                     # optional conditions
    conversation_type: channel    # only in channels, not DMs
```

### Policy Resolution

Policies are evaluated in priority order:

```
1. EXPLICIT DENY     — any explicit deny wins (highest priority)
2. SUBJECT-SPECIFIC  — user policy > team policy > chat policy
3. OBJECT-SPECIFIC   — specific object > wildcard
4. DEFAULT DENY      — if no policy matches, deny (fail-closed)
```

### Built-in Roles (Predefined Policy Sets)

| Role | Privileges | Typical Assignment |
|------|-----------|-------------------|
| **Bot Admin** | `admin` on ALL objects | Configured via `BOT_ADMIN_AAD_IDS` |
| **Domain Admin** | `admin` on specific domains, `read`+`write` on domain data, `use` on all employee tools | Listed in domain YAML `access.admins` |
| **Teamwork** | `read`+`write` on teamwork domain, `use` on standard + shared OAuth tools | Team group chats |
| **Employee** | `read` on assigned domains, `write` on own chat data + domain facts, `use` on allowed tools | Any MS tenant user |
| **External** | Restricted: no filesystem, no credentials, no PM tools | Third-party / external chats |
| **Guest** | `read` on explicitly granted domains, `use` on minimal tools | Non-tenant users in Teams chats |

### Role → Policy Expansion

**Bot Admin** expands to:
```yaml
- {privilege: admin, object: {type: "*", id: "*"}, effect: allow}
```

**Employee in #devops channel** expands to:
```yaml
# From user's role (Employee):
- {privilege: read, object: {type: chat_data, id: "own"}, effect: allow}
- {privilege: write, object: {type: chat_data, id: "own"}, effect: allow}
- {privilege: use, object: {type: tools, id: "mcp__bot-messaging__*"}, effect: allow}
- {privilege: use, object: {type: tools, id: "mcp__bot-memory__*"}, effect: allow}

# From chat's harness (engineering):
- {privilege: read, object: {type: knowledge_domain, id: "eng-runbooks"}, effect: allow}
- {privilege: read, object: {type: knowledge_domain, id: "company-policies"}, effect: allow}
- {privilege: write, object: {type: domain_data, id: "eng-runbooks"}, effect: allow}

# From user's team membership (Engineering team):
- {privilege: use, object: {type: tools, id: "mcp__bot-services__browser_*"}, effect: allow}
```

## Harness as RBAC Policy Container

The harness becomes the primary RBAC policy container for chats:

```yaml
harness:
  label: engineering
  
  # RBAC: Role assignments for this harness
  rbac:
    # What knowledge domains are accessible
    domains:
      - id: eng-runbooks
        privileges: [read, write]     # search + store facts
      - id: company-policies
        privileges: [read]            # search only
    
    # What tools are available
    tools:
      allow:                          # prefix whitelist
        - "mcp__bot-messaging__*"
        - "mcp__bot-memory__*"
        - "mcp__bot-services__search_knowledge"
        - "mcp__bot-services__browser_*"
      deny:                           # explicit deny (overrides allow)
        - "mcp__bot-services__deploy_*"
    
    # What credential scopes are accessible
    credentials:
      allow: ["google:andrew.burban@fora.travel"]
      deny: ["google:admin@company.com"]
    
    # Filesystem access
    filesystem:
      type: sandbox                   # sandbox | full | none
      sandbox_dir: "/app/data/harness-projects/engineering/"
    
    # Network access
    network:
      type: sandboxed                 # sandboxed | full | none
      # sandboxed = TCP 80/443 via proxy, no internal services
    
    # Data scope
    data:
      chat_data: own                  # own | all (admin only)
      legacy_data: false              # see empty-chat_id data? (admin only)
```

## Implementation Strategy

### Phase 1: Policy Data Model (SQLite-backed)

```sql
-- Roles: named sets of privileges
CREATE TABLE rbac_roles (
    role_id TEXT PRIMARY KEY,           -- 'bot_admin', 'employee', 'guest', 'eng-domain-admin'
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    is_builtin BOOLEAN DEFAULT FALSE,   -- built-in roles can't be deleted
    created_at TEXT NOT NULL
);

-- Policies: individual privilege grants/denials
CREATE TABLE rbac_policies (
    policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id TEXT NOT NULL,              -- which role this policy belongs to
    privilege TEXT NOT NULL,            -- admin | read | write | use | execute
    object_type TEXT NOT NULL,          -- knowledge_domain | credentials | tools | chat_data | filesystem | network
    object_id TEXT DEFAULT '*',         -- specific object or '*' for all
    effect TEXT DEFAULT 'allow',        -- allow | deny
    conditions TEXT DEFAULT '{}',       -- JSON conditions (conversation_type, tenant_id, etc.)
    FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id)
);

-- Role assignments: who has which role
CREATE TABLE rbac_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,         -- user | team | chat
    subject_id TEXT NOT NULL,           -- AAD ID, team ID, or chat ID
    role_id TEXT NOT NULL,
    granted_by TEXT DEFAULT '',         -- who granted this
    granted_at TEXT NOT NULL,
    expires_at TEXT,                    -- optional expiry
    FOREIGN KEY (role_id) REFERENCES rbac_roles(role_id)
);

-- NOTE: Harness→role mapping is handled by RBAC policies (subject_type=chat),
-- not a dedicated join table. The harness assigns a chat to roles via policies.

CREATE INDEX idx_rbac_assignments_subject ON rbac_assignments(subject_type, subject_id);
CREATE INDEX idx_rbac_policies_role ON rbac_policies(role_id);
```

### Phase 2: Policy Evaluation Engine

```python
# bot/rbac/engine.py

class PolicyEngine:
    """Evaluates RBAC policies for access decisions."""
    
    async def check_access(
        self,
        subject: Subject,        # user_id + team_ids + chat_id
        privilege: str,          # "read" | "write" | "use" | "execute" | "admin"
        object_type: str,        # "knowledge_domain" | "tools" | etc.
        object_id: str,          # specific object or "*"
    ) -> AccessDecision:
        """Evaluate whether subject has privilege on object.
        
        Resolution order:
        1. Collect all roles for: user + user's teams + user's chat harness
        2. Collect all policies from those roles
        3. Check for EXPLICIT DENY (highest priority)
        4. Check for ALLOW (most specific wins)
        5. Default: DENY
        
        Returns: AccessDecision(allowed: bool, reason: str, policy_id: int | None)
        """
```

### Phase 3: Hook Integration

Replace the current 7-layer enforcement in hooks.py with:

```python
# In PreToolUse hook:
decision = await policy_engine.check_access(
    subject=current_subject,
    privilege="use",
    object_type="tools",
    object_id=tool_name,
)
if not decision.allowed:
    return {"error": decision.reason}
```

### Phase 4: MCP Tools for RBAC Management

```
manage_rbac(action, ...)
  - list_roles: show all roles + policies
  - create_role: new role with policies
  - assign_role: assign to user/team/chat
  - revoke_role: remove assignment
  - check_access: test if subject has privilege on object
  - audit_log: recent access decisions
```

## Migration Path

1. **Phase 1**: Define data model + built-in roles + seed policies from current harness configs
2. **Phase 2**: Build policy engine alongside existing hooks (dual enforcement during migration)
3. **Phase 3**: Route hooks.py checks through policy engine (one layer at a time)
4. **Phase 4**: Remove legacy enforcement code, policy engine becomes the single authority
5. **Phase 5**: MCP tools for admin RBAC management + audit dashboard

## Current State → RBAC Mapping

| Current Mechanism | RBAC Equivalent |
|------------------|----------------|
| `BOT_ADMIN_AAD_IDS` | Built-in `bot_admin` role assigned to specific users |
| `domain.yaml access.admins` | Domain `owner` field (checked via `is_domain_admin()`) |
| `harness.allowed_tools` | `use` policies on `tools` object type |
| `harness.allowed_emails` | `use` policies on `credentials` object type |
| `harness.knowledge_domains` | `read`/`write` policies on `knowledge_domain` objects |
| `harness.data_scope` | `read`/`write` policies on `chat_data` + `legacy_data` |
| `harness.sandbox` | `execute` policies on `filesystem` + `network` |
| Non-admin harness | `employee` role (restricts tool access) |
| `_ADMIN_ONLY_TOOLS` list | `use` deny policies for non-admin roles |
| `_EXTERNAL_CHAT_BLOCKED_PREFIXES` | `use` deny policies for employee role |
| Per-chat tool overrides | Role assignment overrides at chat level |

## Design Decisions

1. **Harness = role container** — harnesses don't go away; they become the primary way
   admins assign roles to chats. The harness config translates to RBAC policies.

2. **SQLite-backed** — no external dependency (OPA, Casbin). Simple enough for 100-500
   users. Policies cached in memory with TTL refresh.

3. **Deny overrides allow** — any explicit deny policy wins. This matches Azure AD and
   AWS IAM behavior. Prevents accidental privilege escalation.

4. **Default deny** — no policy match = denied. Fail-closed. This is the current behavior
   (hooks.py denies by default for non-admin).

5. **Incremental migration** — dual enforcement during transition. New policy engine runs
   alongside existing hooks. Discrepancies logged. Once stable, legacy code removed.

6. **Team inheritance** — users inherit team privileges. This enables "Engineering team
   gets access to eng-runbooks domain" without per-user configuration.

7. **Audit trail** — every access decision logged (allow and deny). Required for
   compliance (SOC 2, ISO 27001).

---

## Harness Config vs RBAC Security — Clean Separation

### Principle

The **harness** is a UX/behavior configuration profile (how the bot behaves in a chat).
**RBAC** is the security enforcement layer (what the bot is ALLOWED to do).

The harness says "use Opus model with coding modules". RBAC says "this user can execute
bash commands in a sandboxed filesystem with network access".

### Field Classification

| Harness Field | Category | Stays in Harness? | Moves to RBAC? | Rationale |
|--------------|----------|------------------|----------------|-----------|
| `label` | Identity | ✅ | | Harness identifier |
| `model` | Behavior | ✅ | | AI model selection = UX choice |
| `effort` | Behavior | ✅ | | Thinking depth = UX choice |
| `max_turns` | Behavior | ✅ | | Iteration limit = cost control |
| `max_budget_usd` | Behavior | ✅ | | Budget = cost control |
| `output_token_cap` | Behavior | ✅ | | Output limit = cost control |
| `prompt_modules` | Behavior | ✅ | | Prompt module filter (domain:component) |
| `claude_md` | Behavior | ✅ | | CLAUDE.md path = behavior |
| `claude_md_modules` | Behavior | ✅ | | Prompt modules = behavior |
| `custom_instructions` | Behavior | ✅ | | Prompt instructions = behavior |
| `facts_visible` | Behavior | ✅ | | Prompt content visibility |
| `facts_categories` | Behavior | ✅ | | Prompt content filtering |
| `project_dir` | Behavior | ✅ | | Working directory = behavior |
| `settings_json` | Behavior | ✅ | | SDK settings = behavior |
| `query_timeout_s` | Behavior | ✅ | | Timeout = operational |
| `fast_mode` | Behavior | ✅ | | Speed mode = cost/UX |
| `write_domain` | Behavior | ✅ | | Domain tagging = behavior |
| `data_scope` | **REMOVED** | Replaced by RBAC `read` on `chat_data` |
| `allowed_tools` | **SECURITY** | ❌ Remove | ✅ `use` on `tools` | Controls tool access |
| `allowed_emails` | **SECURITY** | ❌ Remove | ✅ `use` on `credentials` | Controls credential access |
| `allowed_credentials` | **SECURITY** | ❌ Remove | ✅ `use` on `credentials` | Controls credential access |
| `knowledge_domains` | **SECURITY** | ❌ Remove | ✅ `read`/`write` on `knowledge_domain` | Controls domain access |
| `sandbox` | **SECURITY** | ❌ Remove | ✅ `execute` on `filesystem` | Controls code execution |
| `no_network` | **SECURITY** | ❌ Remove | ✅ `execute` on `network` | Controls network access |

### Three-Way Separation: Harness / Knowledge Domains / RBAC

Each concern has a single owner. No overlap.

| Field | Owner | Rationale |
|-------|-------|-----------|
| `model` | **Harness** | AI behavior |
| `effort` | **Harness** | AI behavior |
| `max_turns` | **Harness** | Cost control |
| `max_budget_usd` | **Harness** | Cost control |
| `output_token_cap` | **Harness** | Cost control |
| `fast_mode` | **Harness** | Performance/cost |
| `prompt_modules` | **Harness** | Prompt module filter |
| `claude_md` | **Harness** | Custom CLAUDE.md path |
| `custom_instructions` | **Harness** | Prompt behavior |
| `settings_json` | **Harness** | SDK config |
| `project_dir` | **Harness** | Working directory |
| `query_timeout_s` | **Harness** | Operational |
| `claude_md_modules` | **Domain** | Domain-specific prompt modules (already `prompt_modules` on domain) |
| `facts_categories` | **Domain** | Domain-owned fact categories (already `facts_categories` on domain) |
| `facts_visible` | **Domain** | Per-domain fact visibility |
| `write_domain` | **Domain** | Domain routing for writes |
| `data_scope` | **RBAC** | Data access level |
| `allowed_tools` | **RBAC** | Tool permissions |
| `allowed_emails` | **RBAC** | Credential permissions |
| `allowed_credentials` | **RBAC** | Credential permissions |
| `knowledge_domains` | **RBAC** | Domain access (read/write) |
| `sandbox` | **RBAC** | Filesystem execution scope |
| `no_network` | **RBAC** | Network execution scope |

### Resulting Split

**Harness (pure AI behavior — 13 fields):**
```python
@dataclass
class Harness:
    label: str = "default"
    # AI model & behavior
    model: str | None = None
    effort: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    output_token_cap: int | None = None
    fast_mode: bool | None = None
    # Prompt composition (harness-level overrides)
    prompt_modules: list[str] | None = None  # domain:component filter
    claude_md: str | None = None
    custom_instructions: str | None = None
    # SDK / project
    settings_json: str | None = None
    project_dir: str | None = None
    query_timeout_s: int | None = None
```

**Knowledge Domain (carries its own config — already implemented):**
```python
@dataclass
class DomainKnowledge:
    prompt_modules: list[str]       # system prompt modules for this domain
    skills: list[str]               # relevant skills
    auto_load_skills: list[str]     # pre-loaded into CLAUDE.md
    facts_categories: list[str]     # fact categories owned
    essential_docs: list[str]       # always in hot summary
    hot_context_budget: int = 3000  # chars for hot summary
    # NEW (moved from harness):
    facts_visible: bool = True      # show this domain's facts in prompt
```

**RBAC determines which domains a session accesses.** The domain itself carries
all the knowledge config. When RBAC grants `read` on `knowledge_domain:eng-runbooks`,
the session automatically gets eng-runbooks' prompt_modules, skills, facts_categories,
and hot summary — all composed by the domain composition layer.

Harness `claude_md_modules` is a filter (allowlist) on domain offerings, not the
primary source. Modules come from the domains the RBAC role grants access to.
`prompt_modules` (replaces removed `prompt_includes/excludes`) filters domain-
contributed system prompt modules using the same domain:component notation.

**How prompt composition works after separation:**
```
System prompt = 
    base prompts (from data/prompts/*.txt)
  + domain custom_instructions (from all RBAC-granted domains)
  + UNION of domain.prompt_modules (filtered by harness.prompt_modules)
  + UNION of domain.auto_load_skills (filtered by harness.claude_md_modules)
  + domain awareness section (hot summaries for all granted domains)
  + domain facts (categories from all granted domains, visible if domain.facts_visible)
```

**RBAC Roles (security — assigned to users/teams/chats):**
```yaml
role: engineering-employee
permissions:
  # Data access
  - {action: read, object: chat_data, scope: own}
  - {action: write, object: chat_data, scope: own}
  # Knowledge domains
  - {action: read, object: knowledge_domain, id: eng-runbooks}
  - {action: read, object: knowledge_domain, id: company-policies}
  - {action: write, object: domain_data, id: eng-runbooks}  # store facts
  # Tools
  - {action: use, object: tools, id: "mcp__bot-messaging__*"}
  - {action: use, object: tools, id: "mcp__bot-memory__*"}
  - {action: use, object: tools, id: "mcp__bot-services__search_knowledge"}
  - {action: use, object: tools, id: "mcp__bot-services__browser_*"}
  - {action: deny, object: tools, id: "deploy_*"}
  # Credentials
  - {action: use, object: credentials, id: "google:eng-team@company.com"}
  # Execution
  - {action: execute, object: filesystem, id: sandbox}
  - {action: execute, object: network, id: sandboxed}
```

**How they connect:** A chat is assigned BOTH a harness (behavior) AND roles (security).
The harness says "use Opus with coding modules". The role says "can execute bash in sandbox,
search eng-runbooks, use browser tools".

```
Chat: #devops-channel
  ├── Harness: "engineering" (model=opus, modules=[deploy,staging], fast_mode=true)
  └── Roles: ["engineering-employee"] (domains, tools, sandbox, network)
```

### Complete Enforcement Point → RBAC Policy Mapping

Every current enforcement point in hooks.py maps to an RBAC policy:

| Current Layer | hooks.py Lines | What It Checks | RBAC Equivalent |
|--------------|---------------|----------------|-----------------|
| **L1a: Admin suppression** | 812-813 | `is_admin` from RBAC execution scope | Admin determined by RBAC role assignment |
| **L1a+: Deploy gates** | 691-804 | Admin + /deploy command + review flags | `{action: use, object: tools, id: "deploy_*"}` + workflow state |
| **L1b: Admin tool gate** | 820-825 | `_effective_admin` for 34 tools | `{action: use, object: tools, id: "<tool_name>"}` per role |
| **L1b2: Harness allowed_tools** | 840-854 | Tool prefix in `allowed_tools` list | `{action: use, object: tools, id: "<prefix>*"}` per role |
| **L1b3: Email account gate** | 862-898 | `user_google_email` in `allowed_emails` | `{action: use, object: credentials, id: "google:<email>"}` |
| **L1c: External chat blocking** | 918-949 | 30+ blocked prefixes for externals | `{action: deny, object: tools, id: "<prefix>*"}` on employee role |
| **L1c.2: Chat isolation** | 957-985 | Target chat_id == origin chat_id | `{action: use, object: chat_data, scope: own}` (structural) |
| **L2: File path protection** | 996-1059 | Path within sandbox / not in blocklist | `{action: execute, object: filesystem, id: "sandbox\|full"}` |
| **L2b: File-send path** | 488-539 | Path within allowed dirs for file sending | Same as L2 — filesystem scope |
| **L3: Bash blocklist** | 1066, 374-455 | Command not in blocklist | `{action: execute, object: tools, id: "Bash"}` + execution scope |
| **L3b: Bwrap sandbox** | 1077-1095 | Kernel namespace isolation | `{action: execute, object: filesystem, id: "sandbox"}` enforcement |
| **AccessContext: chat data** | access.py 54-91 | `allowed_chat_ids` filter | `{action: read, object: chat_data, scope: own\|all}` |
| **AccessContext: domains** | access.py 141-173 | `allowed_domains` filter | `{action: read, object: knowledge_domain, id: "<domain>"}` |
| **Domain admin check** | mcp/domains.py 47-69 | `is_domain_admin()` for destructive ops | `{action: admin, object: knowledge_domain, id: "<domain>"}` |
| **Browser pool budget** | hooks.py 1115-1141 | Tier-based tab limits | `{action: use, object: tools, id: "browser_*", conditions: {max_tabs: N}}` |

### What RBAC Replaces Entirely

These hooks.py mechanisms become **dead code** after RBAC is fully wired:

1. `_ADMIN_ONLY_TOOLS` set → replaced by role permissions on tools
2. `_EXTERNAL_CHAT_BLOCKED_PREFIXES` → replaced by employee role denials
3. `_BLOCKED_PATH_PATTERNS` / `_ADMIN_BLOCKED_PATH_PATTERNS` → replaced by filesystem scope
4. `_BASH_BLOCKLIST` / `_ADMIN_BASH_BLOCKLIST` → replaced by execute permissions + scope
5. `_CHAT_ISOLATED_TOOLS` → replaced by chat_data scope (structural, always own)
6. `_EMAIL_GATED_PREFIXES` → replaced by credential permissions
7. `_EXPLICIT_HARNESS_TOOLS` → replaced by tool permissions (deny by default)
8. `data_scope` field on harness → replaced by chat_data read/write permissions
9. `allowed_tools` field on harness → replaced by tool use permissions
10. `allowed_emails` field on harness → replaced by credential use permissions
11. `knowledge_domains` field on harness → replaced by knowledge_domain read/write permissions
12. `sandbox` / `no_network` fields → replaced by filesystem/network execute permissions

### What Stays Outside RBAC

These are NOT security decisions — they're operational/behavioral:

1. **Deploy workflow gates** (deploy_review_passed, qa_staging_passed) — workflow state, not permission
2. **Rate limiting** — operational protection, not access control
3. **Read size guard** (500KB) — SDK protection, not permission
4. **Browser pool capacity** — resource management, not permission (though tab limits could be RBAC)
5. **Bwrap availability check** — runtime capability, not permission

### Harness ↔ RBAC Connection

When a chat is created, it gets:
1. **Harness** (from harness_rules or admin assignment) → controls behavior
2. **Role(s)** (from RBAC assignments on chat + user + user's teams) → controls access

The harness rules engine maps Teams conversations to harnesses (behavior).
RBAC role assignments map users/teams/chats to roles (security).

```yaml
# Harness rule (behavior):
- match: {team_name: "Engineering*"}
  harness: engineering        # model, modules, prompt config

# RBAC assignment (security):
- subject: {type: team, id: "engineering-group"}
  role: engineering-employee  # domains, tools, sandbox, network
```

Both can be assigned via the same admin command:
```
/setup-channel #devops harness=engineering role=engineering-employee
```

### Migration Checklist

| Step | What | Effort | Status |
|------|------|--------|--------|
| 1 | Create RBAC schema + policy engine (`bot/rbac/`) | 🔴 Large | DONE (Sprint R1) |
| 2 | Define built-in roles (admin, employee, guest) + seed policies | 🟡 Medium | DONE (Sprint R2) |
| 3 | Map current harness security fields → RBAC policies | 🟡 Medium | DONE (Sprint R2) |
| 4 | Add RBAC check to hooks.py PreToolUse (alongside existing checks) | 🟡 Medium | DONE (Sprint R3) |
| 5 | Add RBAC check to AccessContext construction | 🟡 Medium | DONE (Sprint R3) |
| 6 | Add RBAC check to domain access in search_knowledge | 🟢 Small | DONE (Sprint R3) |
| 7 | Verify dual enforcement + RBAC_PRIMARY feature flag | 🔴 Large | DONE (Sprint R4) |
| 8 | Remove security fields from Harness dataclass | 🟡 Medium | DONE (Sprint R4) |
| 9 | Simplify legacy enforcement code in hooks.py (wrapped in `if not _RBAC_PRIMARY:`) | 🔴 Large | DONE (Sprint R5) |
| 10 | Admin MCP tools for RBAC management (`manage_rbac`) | 🟡 Medium | DONE (Sprint R5) |
| 11 | Documentation updates + legacy constant cleanup | 🟡 Medium | DONE (Sprint R5) |

### Sprint Summary

| Sprint | Focus | Key Deliverables |
|--------|-------|-----------------|
| **R1** | Data Model | `bot/rbac/` package: models, schema, store, engine. SQLite tables for roles, policies, assignments, audit log. |
| **R2** | Seeding | Built-in roles (bot_admin, employee, teamwork). Harness security fields mapped to RBAC policies. Auto-seed on startup. |
| **R3** | Dual Enforcement | RBAC check in PreToolUse hook (alongside legacy). RBAC in AccessContext + search_knowledge. Audit logging for every decision. |
| **R4** | RBAC Primary | `RBAC_PRIMARY` feature flag (default: true). RBAC is authoritative; legacy runs log-only. Harness security fields removed. Execution scope (filesystem/network) from RBAC. |
| **R5** | MCP Tools + Cleanup | `manage_rbac` MCP tool (admin-only). Legacy Layers 1b/1b2/1b3/1c wrapped in `if not _RBAC_PRIMARY:`. Legacy constants marked. Documentation updated. |
