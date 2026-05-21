# Corporate Bot — Access Management & Security Architecture

> **Note:** This document describes the credential tier model. For the current RBAC-based
> permission model (flat per-chat roles), see [ACCESS_MODEL.md](ACCESS_MODEL.md).

## Credential Tiers

The bot manages three tiers of credentials, each with different scope and lifecycle:

### Tier 1: System Credentials (bot-level, admin-managed)

Shared service accounts used by the bot itself. Configured in `.env`.
Available to all sessions unless restricted by harness `allowed_emails`.

| Credential | Scope | How Used |
|-----------|-------|----------|
| `ANTHROPIC_API_KEY` | All sessions | Claude SDK (every query) |
| `GEMINI_API_KEY` | All sessions | Embeddings, media descriptions |
| `TEAMS_APP_ID/SECRET` | Teams handler | JWT validation, bot token |
| `GITHUB_TOKEN` | Domain sync | Fetch repo content |
| `TOKEN_ENCRYPTION_KEY` | Token vault | Encrypt/decrypt user tokens |

**Management:** `.env` file. Changes require container restart.
**Security:** Never exposed in chat. Hooks blocklist prevents reading `.env`.

### Tier 2: Shared Service Credentials (harness-level, admin-managed)

Service accounts attached to specific harnesses for team-level access.
Example: HR team harness has access to the shared HR inbox.

| Example | Harness | What It Accesses |
|---------|---------|-----------------|
| `hr-inbox@company.com` | `hr-team` | Shared HR mailbox via Google Workspace |
| `ops-alerts@company.com` | `devops` | Ops monitoring inbox |
| `reports@company.com` | `finance` | Financial reports inbox |

**Management:**
```
# Add a shared credential:
manage_oauth(action="add", label="hr-inbox", token="...")

# Attach to harness:
manage_harness(action="update", label="hr-team",
  fields='{"allowed_emails": ["hr-inbox@company.com"]}')
```

**Security:**
- Controlled by `allowed_emails` on the harness
- Only sessions using that harness can access that email
- Hooks enforce at Layer 1b3 (fail-closed)

### Tier 3: Personal Credentials (per-user, SSO-acquired)

Each employee's own MS365 token, acquired via Teams SSO (On-Behalf-Of flow).
Only accessible in that user's DM session.

| Data | Storage | Encryption |
|------|---------|-----------|
| Access token | `user_tokens` SQLite table | Fernet (AES-128-CBC) |
| Refresh token | `user_tokens` SQLite table | Fernet |
| Scopes | `user_tokens` SQLite table | Plaintext (non-sensitive) |

**Lifecycle:**
1. User opens DM with bot → Teams sends SSO token
2. Bot exchanges via OBO flow → stores encrypted in vault
3. DM queries use user's own token for Graph API
4. Scheduler pre-refreshes every ~50 min
5. Token expires after 90 days inactive → user re-consents

**Security:**
- Encrypted at rest (Fernet with `TOKEN_ENCRYPTION_KEY`)
- Only accessible in that user's DM session (chat_id isolation)
- Never logged or returned in plaintext
- GDPR deletion via `delete_user_data(aad_object_id)`

## Permission Model

### 4-Tier Access Control

```
┌─────────────────────────────────────────────┐
│  TIER 1: BOT ADMIN                          │
│  Config: BOT_ADMIN_AAD_IDS env var          │
│  Access: Everything — code, deploy, all     │
│  domains, all tools, all credentials        │
├─────────────────────────────────────────────┤
│  TIER 2: DOMAIN ADMIN                       │
│  Config: domain.yaml admins list            │
│  Access: Manage OWN domain content only.    │
│  Cannot: modify bot code, deploy, manage    │
│  other domains, access system credentials   │
├─────────────────────────────────────────────┤
│  TIER 3: EMPLOYEE                           │
│  Config: Default for all tenant users       │
│  Access: Query allowed domains, use tools   │
│  per harness, personal MS365 in DM          │
│  All Bash sandboxed (bwrap kernel isolation) │
├─────────────────────────────────────────────┤
│  TIER 4: GUEST                              │
│  Config: Users from different Azure tenant  │
│  Access: search_knowledge only (read-only)  │
│  No personal data, no tools, no code exec   │
└─────────────────────────────────────────────┘
```

### How Tiers Are Resolved

```
User sends message
  → Extract aad_object_id from Teams Activity
  → Check BOT_ADMIN_AAD_IDS → Tier 1?
  → Check tenant_id matches TEAMS_TENANT_ID → if not, Tier 4 (Guest)
  → Default → Tier 3 (Employee)
  → Per-domain: check domain.yaml admins → Tier 2 for that domain
```

### Enforcement Layers (defense-in-depth)

| Layer | What It Checks | Where |
|-------|---------------|-------|
| **1. Tier resolution** | User tier from AAD ID | `bot/core/roles.py` |
| **2. Harness tool whitelist** | `allowed_tools` prefix list | `hooks.py` Layer 1b2 |
| **3. Email allowlist** | `allowed_emails` per harness | `hooks.py` Layer 1b3 |
| **4. Domain scoping** | `allowed_domains` per harness | `bot/core/access.py` |
| **5. Chat isolation** | `allowed_chat_ids` per session | `bot/core/access.py` |
| **6. Sandbox** | bwrap kernel namespace | `hooks.py` Layer 3b |
| **7. Per-domain auth** | `is_domain_admin()` in tools | `bot/mcp/domains.py` |

### Harness Configuration

Each Teams channel/chat is assigned a harness that controls:

```yaml
harness: engineering
  # What domains can this team search?
  domains: [eng-runbooks, shared-policies]
  # Where does this chat's data get stored?
  write_domain: eng-runbooks            # null = chat-local only
  # RBAC role auto-assigned when harness is set on a chat
  default_role: teamwork
  # What credentials can tools use?
  allowed_credentials: [ops-jira, github-eng]   # references credential_store IDs
```

### Data Flow (domain-centric)

```
write_domain: null         → data stays chat-local (only this session sees it)
write_domain: "eng"        → data tagged with domain_id="eng"
                             → visible to ALL sessions with "eng" in domains
domains: null              → admin, sees ALL domain data
domains: ["eng"]           → sees only eng domain + own chat-local data
```

### Permission Review Commands

Bot admins can review the permission landscape:

```
/harnesses                     — List all harnesses with domains + emails
/harnesses engineering         — Show detailed config for one harness
/chats                         — Show all chats with assigned harnesses
manage_domain(action="health") — Domain health + access summary
```

## Data Isolation

### Knowledge Domain Isolation

```
Employee in #engineering channel
  → Harness: engineering
  → domains: [eng-runbooks, shared-policies]
  → search_knowledge("deploy process")
  → SQL: WHERE domain_id IN ('eng-runbooks', 'shared-policies')
  → CANNOT see: hr-policies, finance-docs, other team data
```

### Personal Data Isolation

```
Employee A in DM
  → Token vault: encrypted token for A's aad_object_id
  → Graph API: Mail.Read with A's token
  → Sees: A's inbox only
  → CANNOT see: B's inbox, shared inboxes (unless harness grants)
```

### Conversation Isolation

```
#engineering channel messages → chat_id = teams:{conv_id_1}
#hr channel messages         → chat_id = teams:{conv_id_2}
Employee A DM               → chat_id = teams:{conv_id_3}

Each session's AccessContext:
  allowed_chat_ids = (own chat_id only)
  allowed_domains = (from harness)
  → Messages, facts, RAG chunks are chat-scoped
  → No cross-chat data leakage
```

## Audit Trail

Every Teams message generates an audit event:

```
audit_log:
  timestamp, user_id, user_email, user_name,
  conversation_id, conversation_type,
  action, details, domains_accessed, tools_used,
  tokens_used, cost_usd, status, error
```

**Review:** `get_audit_summary(hours=24)` — per-team cost + activity
**Export:** `export_audit_csv(hours=168)` — CSV for compliance
**GDPR:** `delete_user_data(aad_object_id)` — purge all PII

## Setup Checklist

### For Bot Admin (initial setup)

1. Set `BOT_ADMIN_AAD_IDS` in `.env` (your Azure AD Object ID)
2. Set `TOKEN_ENCRYPTION_KEY` (generate Fernet key)
3. Set `TEAMS_APP_ID/SECRET/TENANT_ID` (from Azure portal)
4. Deploy container + upload Teams manifest
5. Create harnesses for each team (`manage_harness`)
6. Set up harness assignment rules (`bot/harness_rules.py`)
7. Register knowledge domains (YAML configs in `data/domains/`)

### For Adding a New Team

1. Create harness: `manage_harness(action="create", label="team-x", fields='{"domains":["team-x-docs"], "write_domain":"team-x-docs", "default_role":"teamwork"}')`
2. Add harness rule: map Teams channel name → harness label
3. Register domain: create `data/domains/team-x.yaml` with GitHub repo
4. Trigger initial sync: `manage_domain(action="reindex", domain_id="team-x-docs")`

### For Adding Shared Credentials

1. Add OAuth profile: `manage_oauth(action="add", label="shared-inbox")`
2. Attach to harness: `manage_harness(action="update", label="team-x", fields='{"allowed_emails":["shared@company.com"]}')`
3. Verify: `/harnesses team-x` should show `emails=shared@company.com`

### For Employee Self-Service

1. Open DM with bot in Teams
2. Bot sends welcome card with SSO prompt
3. Click "Authorize" → Teams SSO handles consent
4. Personal email/calendar now accessible in DM
5. No action needed from admin
