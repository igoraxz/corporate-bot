# Corporate Bot — Architecture Reference

Additions to the base architecture for corporate/multi-tenant deployment.

## New Modules

```
bot/
  auth/
    __init__.py         — Auth package
    token_vault.py      — Per-user encrypted OAuth vault (Fernet + SQLite)
  core/
    access.py           — Extended: allowed_domains field + _domain_sql_filter()
  storage/
    domains.py          — Knowledge domain registry, chunking, indexing, search
    github_sync.py      — GitHub API sync, webhook handler, polling fallback
    doc_parser.py       — Multi-format document parser (PDF/DOCX/MD/code)
  mcp/
    domains.py          — MCP tools: manage_domain, search_knowledge, index_document
  harness_rules.py      — Auto-assignment rules engine (Teams → harness)
  teams_cards.py        — Adaptive Cards (welcome, auth, search, status, error)
  teams_files.py        — OneDrive/SharePoint file sharing
  audit.py              — Compliance audit logging + GDPR deletion
teams-app/
  manifest.json         — Teams app manifest v1.19
deploy/
  helm/values.yaml      — Kubernetes Helm chart values
docs/
  SETUP_GUIDE.md        — Zero-to-running deployment guide
  CORPORATE_ARCHITECTURE.md — This file
tests/
  test_domains.py       — 30+ unit tests for corporate features
  corporate_staging_scenarios.json — 10 staging QA scenarios
```

## Knowledge Domain System

### Data Flow

```
GitHub Repo → Webhook/Poll → Content Fetch → Parse → Chunk → Embed → Store
                                                                        ↓
User Query → Resolve allowed_domains → domain_id IN filter → Cosine Sim → Results
```

### Tables

- `domain_registry`: domain configs, sync status, chunk counts
- `domain_documents`: per-file tracking (content hash, last indexed)
- `rag_chunks`: extended with `domain_id` + `source_file` columns

### AccessContext Extension

```python
@dataclass(frozen=True)
class AccessContext:
    chat_id: str = ""
    is_admin: bool = False
    allowed_chat_ids: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()  # NEW: knowledge domain scope
```

`_domain_sql_filter()` generates SQL WHERE clause for domain-scoped queries:
- Admin (empty allowed_domains): no filter
- Employee (specific domains): `domain_id IN (...) OR domain_id = ''`
- No domains: only non-domain data visible

## Permission Tiers

```
Tier 1: BOT_ADMIN    — Full access (from BOT_ADMIN_AAD_IDS env var)
Tier 2: removed (domains use owner field)
Tier 3: EMPLOYEE     — Sandboxed, domain-scoped (default for tenant users)

```

Resolution: `get_user_tier(aad_object_id, tenant_id, source)` in RBAC engine (`bot/rbac/engine.py`)

## Token Vault

Per-user MS365 tokens encrypted with Fernet (AES-128-CBC + HMAC-SHA256):
- Key: `TOKEN_ENCRYPTION_KEY` env var
- Storage: `user_tokens` SQLite table
- Lifecycle: OBO acquisition → encrypted storage → pre-refresh → cleanup

## Harness Rules Engine

Auto-assigns harnesses to new Teams conversations:
- Rules stored in `harness_rules.json` (JsonConfigStore)
- Priority-ordered, first match wins
- Match criteria: conversation_type, team_name, channel_name, tenant_id
- Glob patterns for name matching (fnmatch)

## Config Additions

| Variable | Required | Description |
|----------|----------|-------------|
| `TEAMS_ENABLED` | Yes | Enable Teams bot |
| `TEAMS_APP_ID` | Yes | Entra ID app client ID |
| `TEAMS_APP_SECRET` | Yes | Entra ID app secret |
| `TEAMS_TENANT_ID` | Yes | Azure AD tenant ID |
| `TOKEN_ENCRYPTION_KEY` | For SSO | Fernet encryption key |
| `BOT_ADMIN_AAD_IDS` | Yes | Admin AAD Object IDs |
| `GITHUB_TOKEN` | For domains | PAT for repo content fetch |
| `GITHUB_WEBHOOK_SECRET` | For webhooks | HMAC-SHA256 secret |

## Key Design Decisions

1. **Domains are flat, not hierarchical** — no per-document ACLs, no inheritance.
   Need sub-scoping? Create separate domains.

2. **Raw REST for Teams** (not Agents SDK) — the botbuilder-python SDK is archived,
   Agents SDK is pre-1.0. Raw REST + PyJWT is more stable.

3. **SQLite for domains** (not vector DB) — handles 100K+ documents fine.
   Migrate to pgvector at 500K+ documents.

4. **Fernet for tokens** (not KMS) — simple, no external dependency.
   For regulated industries, swap for Azure Key Vault integration.

5. **Content-hash gating** for sync — SHA-256 per file, skip unchanged.
   Reduces embedding cost by 80-95% on weekly runs.
