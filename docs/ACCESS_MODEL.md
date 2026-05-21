# Corporate Bot — Access Model

How users, chats, harnesses, domains, RBAC, and SSO work together.

## Overview

As of v2.1.0, the access model uses **RBAC (Role-Based Access Control)** as
the primary enforcement layer. The harness remains a UX/behavior configuration
profile (model, effort, prompt modules). Security decisions (which tools, domains,
credentials, and data a session can access) are governed by RBAC roles and policies.

```
User (MS Teams) ──SSO──▶ Bot identifies user
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
              RBAC Role    RBAC Role   RBAC Role
              (bot_admin)  (employee)
                    │         │         │
                    ▼         ▼         ▼
              Chat/Channel assigned a Harness (behavior)
                 + RBAC roles (security)
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
    Knowledge    Tool        Data
    Domains      Access      Scope
    (RBAC)       (RBAC)      (RBAC)
```

**Key change from legacy model**: Security fields previously on the harness
(`data_scope`, `allowed_tools`, `allowed_emails`, `knowledge_domains`, `sandbox`,
`no_network`) are now governed by RBAC role policies. The harness retains 12
behavior-only fields (model, effort, max_turns, prompt composition, etc.).
Legacy enforcement layers (1b, 1b2, 1b3, 1c) are preserved as rollback code
behind `RBAC_PRIMARY=false` but are not active by default.

## 1. Flat Permission Model (CB-79)

**RBAC subject depends on chat type:**

| Context | Subject | Meaning |
|---------|---------|---------|
| **DM** (private chat) | `user:{source}:{user_id}` | User owns their personal workspace |
| **Group/Topic** | `chat:{chat_id}` | ALL users in the chat share the same permissions |

**Canonical roles:**

| Role | Assigned To | Access Level |
|------|-------------|-------------|
| **bot_admin** | Admin user DMs + admin-configured chats | Full access: all data, all tools, all domains, code deploy |
| **teamwork** | Team group chats | R/W teamwork knowledge domain + shared OAuth + standard tools |
| **employee** | Individual user DMs | Read-only teamwork domain + personal workspace |
| **external** | Third-party / external chats | Restricted: no filesystem, no credentials, no PM tools |

**Key principle:** In a group chat, ALL users get identical access — the chat's
assigned role determines everything. Admin role assigned to a user does NOT
apply in group chats. Admin grants roles to chats, not to users-within-chats.

## 2. Chats/Channels — Each Gets a Harness

Every Teams conversation (DM, group chat, or channel) is assigned a **harness** —
a configuration profile that controls what the bot can do in that chat.

**Harness assignment** (`bot/harness_rules.py`):
- Rules engine matches Teams conversation metadata (team name, channel name, conversation type)
- First matching rule wins (priority-ordered)
- Default: `employee` harness for DMs, `employee` for group chats, `employee` for channels
- Admin can override per-chat via `manage_harness` tool

**What a harness controls (behavior-only fields after RBAC migration):**

| Field | What it does | Example |
|-------|-------------|---------|
| `model` | AI model to use | `"claude-sonnet-4-6"` |
| `effort` | Thinking depth | `"high"` |
| `max_turns` | Turn limit per query | `75` |
| `max_budget_usd` | Session budget cap | `5.0` |
| `output_token_cap` | Per-query output limit | `25000` |
| `fast_mode` | SDK fast mode (Opus only) | `true` |
| `domains` | Active knowledge domains | `["admin", "teamwork"]` |
| `write_domain` | R/W domain for fact routing | `"teamwork"` |
| `default_role` | RBAC role auto-assigned on chat | `"teamwork"` |
| `prompt_modules` | Domain prompt module filter | `["deploy", "staging"]` |
| `claude_md` | Custom CLAUDE.md path | `"data/claude_md/eng.md"` |
| `settings_json` | SDK settings overrides | `{"fastMode": true}` |
| `query_timeout_s` | Zombie detection timeout | `900` |
| `pm_project_id` | PM project for this harness | `1` |

**Security fields moved to RBAC** (no longer on harness):
`data_scope`, `allowed_tools`, `allowed_emails`, `knowledge_domains`, `sandbox`, `no_network`.
These are now governed by RBAC role policies assigned to users/teams/chats.

**Canonical harnesses** (7 total, auto-created at startup):

| Harness | Purpose | Typical assignment |
|---------|---------|-------------------|
| `admin` | Bot admins | Admin DMs, admin groups |
| `coding` | Development sessions | Coding relay chats |
| `teamwork` | Team collaboration | Team group chats |
| `personal` | Individual employees | Employee DMs |
| `external_chat` | Third-party conversations | External/3P chats |
| `project-coding` | Project coding sessions | Team coding chats |

## 3. Knowledge Domains — Composable Knowledge Scopes

Each domain is a labeled collection of documents indexed for search.
Harnesses assign domains to chats via the `domains` field — employees in that chat can search those domains.

```
Harness "engineering" ──▶ domains: ["eng-runbooks", "company-policies"]
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              eng-runbooks        company-policies
              ┌──────────┐        ┌──────────────┐
              │ GitHub    │        │ SharePoint   │
              │ repo sync │        │ site sync    │
              │ 47 docs   │        │ 31 docs      │
              │ 850 chunks│        │ 520 chunks   │
              └──────────┘        └──────────────┘
```

**What a domain provides** (9-layer knowledge stack):
1. **Documentation** — Git-backed or SharePoint-synced documents (RAG-indexed)
2. **Knowledge map** — LLM-generated topic summary in the system prompt
3. **System prompt modules** — Domain-specific instructions auto-loaded
4. **Skills** — Relevant capabilities declared (helps AI skill assessment)
5. **Auto-load skills** — Skills pre-loaded into CLAUDE.md
6. **Facts** — Structured knowledge tagged with domain_id
7. **Fact categories** — Which fact categories this domain "owns"

**Domain access control**:
- `domains` on the harness controls which domains a chat can search
- `write_domain` on the harness controls which domain facts are auto-routed to
- Reference domains derived at runtime: `domains - write_domain - core`
- `access.teams` in the domain YAML controls which teams can access the domain
- Admin can access all domains regardless of harness config

## 4. Data Flow — How Access Decisions Are Made

### Message arrives in Teams channel
```
Teams webhook
  │
  ▼
1. JWT validation (Azure AD token)
  │
  ▼
2. Extract user identity (AAD Object ID, tenant ID)
  │
  ▼
3. Resolve RBAC role (flat model: DM=user subject, group=chat subject)
  │
  ▼
4. Look up chat's harness (harness_rules.py → harness.py)
  │
  ▼
5. Build AccessContext:
   ├── is_admin = (RBAC role == bot_admin)
   ├── allowed_chat_ids = (own chat_id only; admin DM = empty/global, admin group = scoped)
   └── allowed_domains = harness.domains (resolved via _resolve_session_domains())
  │
  ▼
6. Build system prompt:
   ├── Base prompt (identity, rules, security)
   ├── Composed modules (harness + domain modules)
   ├── Domain awareness (hot summaries for assigned domains)
   ├── Skills awareness (from domain skills lists)
   └── Custom instructions (from harness)
  │
  ▼
7. Query Claude SDK with AccessContext
   ├── Model can call: search_knowledge (domain-scoped)
   ├── Model can call: memory_search (own chat only)
   ├── Model can call: search_facts (own chat + domain facts)
   └── Tool gating: RBAC role policies + harness allowed_tools whitelist
```

### Data isolation matrix

| Data Type | Admin sees | Employee sees | External sees |
|-----------|-----------|--------------|--------------|
| **Chat messages** | All chats | Own chat only | Own chat only |
| **Chat RAG** | All chats | Own chat only | Own chat only |
| **Facts (chat-local)** | All facts | Own chat's facts | Own chat's facts |
| **Facts (domain-scoped)** | All domains | Assigned domains | None (unless granted) |
| **Domain documents** | All domains | Assigned domains | None (unless granted) |
| **Media cache** | All chats | Own chat only | Own chat only |
| **Legacy data (empty chat_id)** | ✅ Visible | ❌ Hidden | ❌ Hidden |

### Key security rules

1. **Admin access determined by RBAC role** — admin privileges come from RBAC role assignment,
   access level determined by the session's RBAC execution scope.
   This prevents the model from including sensitive data in responses visible to guests.

2. **Employees never see other employees' chat data** — cross-chat knowledge sharing
   happens through knowledge domains (curated docs), not through chat history leakage.

3. **Domain facts are cross-chat** — a fact tagged with `domain_id="eng-runbooks"` is
   visible to ALL chats that have `eng-runbooks` in their harness's `domains`.
   This is intentional — it's how domain knowledge is shared.

4. **Tool gating is per-harness, not per-user** — a bot admin in an employee-harness
   chat gets employee-level tools. The harness controls the chat, not the user.

## 5. SSO Integration

```
Teams message → Bot Framework → JWT token
                                    │
                              ┌─────┴─────┐
                              │ Validate   │
                              │ Azure AD   │
                              │ signature  │
                              └─────┬─────┘
                                    │
                              ┌─────┴─────┐
                              │ Extract:   │
                              │ • aad_id   │
                              │ • tenant   │
                              │ • name     │
                              │ • email    │
                              └─────┬─────┘
                                    │
                                    ▼
                            get_user_tier()
```

**Azure AD app registration requirements:**
- App type: Bot registration (Azure Bot Service)
- Auth: OAuth 2.0 with client secret
- Scopes: `User.Read` (identity), `Mail.Read` (optional), `Calendars.ReadWrite` (optional),
  `Files.Read.All` + `Sites.Read.All` (for SharePoint document indexing)

**No user ID lists needed** — the bot trusts Azure AD to authenticate users.
Admin users are configured via `BOT_ADMIN_AAD_IDS` env var (comma-separated Azure AD Object IDs).
All other tenant users are employees. Non-tenant users are guests.

## 6. Harness Rules — Automatic Channel Assignment

When a new Teams conversation appears, the rules engine assigns a harness:

```yaml
# Example rules (harness_rules.json):
- id: rule-eng-channels
  priority: 10
  match:
    conversation_type: channel
    team_name: "Engineering*"
  harness: engineering          # ← harness with eng domains

- id: rule-hr-dm
  priority: 20
  match:
    conversation_type: personal
    user_aad_id: "hr-lead-aad-id"
  harness: hr-admin             # ← harness with HR admin access

- id: rule-default-dm
  priority: 100
  match:
    conversation_type: personal
  harness: employee             # ← default employee harness
```

Rules are AND-matched (all specified fields must match). First match wins by priority.

## 7. Complete Example

**Scenario**: Engineer "Alice" asks about deploy procedures in the #devops channel.

```
1. Alice sends: "How do I deploy to production?"
   
2. Bot identifies Alice:
   - AAD Object ID: alice-aad-123
   - Tenant: contoso.onmicrosoft.com (= org tenant ✅)
   - Tier: EMPLOYEE

3. #devops channel harness: "engineering"
   - domains: ["eng-runbooks", "company-policies"]
   - write_domain: "eng-runbooks"
   - default_role: "teamwork"

4. AccessContext built:
   - is_admin: false
   - allowed_chat_ids: ("teams:devops-channel-id",)
   - allowed_domains: ("eng-runbooks", "company-policies")

5. System prompt includes:
   - Engineering Runbooks knowledge map (hot summary)
   - Company Policies knowledge map (hot summary)
   - "Use search_knowledge to dive deeper"

6. Claude reads the question, sees domain context, calls:
   search_knowledge(query="deploy to production", domain_filter="eng-runbooks")
   
7. Returns relevant chunks from deploy/pipeline.md

8. Claude answers: "According to the Engineering Runbooks (deploy/pipeline.md):
   To deploy to production, you need..."
```

**What Alice CANNOT do:**
- See messages from other channels (own chat only)
- Search HR-only domains (not in her harness)
- Call admin tools (deploy_bot, manage_harness)
- See Bob's DM facts (data isolation)

**What Alice CAN do:**
- Search eng-runbooks and company-policies domains
- Store facts tagged with domain_id="eng-runbooks"
- Use search_knowledge, memory_search, search_facts
- Use any non-admin MCP tools permitted by her RBAC role
