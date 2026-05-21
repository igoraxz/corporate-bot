# Corporate Bot — Admin Guide

Complete guide for configuring and managing the bot's knowledge system.

## Entity Model

```
Domain ←owns→ Owner (user)
Domain ←provides→ 6 Layers (INSTRUCTIONS, FACTS, KNOWLEDGE, FILES, OPTIMIZATION, CREDENTIALS)
Chat ←harness→ Harness (model + domains + role)
Harness ←default_role→ RBAC Role (auto-assigned)
Harness ←domains→ [Domain, ...]
Harness ←write_domain→ Domain (fact routing)
Harness ←selects from→ Domain.modules (via filters)
```

### Domain
Knowledge provider. Owns skills, prompt modules, facts, docs, credentials.
Each domain has an owner, a GitHub repo (optional), and a default harness preset.

**Canonical domains:**
- `core` — Bot identity. Always present in all chats (implicit). System prompts + canonical skills.
- `admin` — Bot infrastructure. Deploy history, architecture decisions. Admin-only.
- `teamwork` — Team knowledge. Processes, business decisions. Team group chats.

### Harness
Model configurator + knowledge filter + role reference. The **primary config entity**
for a chat. Controls HOW the model behaves, WHICH knowledge it sees, and WHAT role
it auto-assigns:

- Model selection (sonnet/opus/haiku), effort level, max turns, budget
- `domains` — which knowledge domains are active (core always implicit)
- `write_domain` — where FACTS_UPDATE auto-routes
- `default_role` — RBAC role auto-assigned when harness is set on a chat
- `claude_md_modules` — filters auto-load skills into CLAUDE.md
- `prompt_modules` — filters domain prompt modules into system prompt
- `allowed_credentials` — credential gating

**Reference domains** are derived automatically: `domains - write_domain - core`.
No need to configure separately.

**Canonical harnesses (6):**

| Harness | Model | Effort | Domains | Write | Role |
|---------|-------|--------|---------|-------|------|
| `admin` | opus/1M | high | admin, teamwork | admin | bot_admin |
| `coding` | opus/1M | high | admin, teamwork | admin | bot_admin |
| `teamwork` | sonnet | medium | teamwork | teamwork | teamwork |
| `project-coding` | opus/1M | high | teamwork | teamwork | teamwork |
| `personal` | sonnet | medium | (core only) | (local) | employee |
| `external_chat` | sonnet | — | (core only) | (local) | external |

### RBAC Role
Permission set. Auto-assigned by harness, or manually assigned.

| Role | Description | Used by |
|------|-------------|---------|
| `bot_admin` | Full access | admin, coding harnesses |
| `teamwork` | R/W teamwork domain + standard tools | teamwork, project-coding |
| `employee` | Sandbox + personal workspace | personal |
| `external` | Restricted — no filesystem, no creds, no PM | external_chat |

**Flat permission model:** DM = user subject. Group/topic = chat subject.
Admin role assigned to a user does NOT apply in group chats.

### Knowledge Filter Notation
Used by `claude_md_modules` and `prompt_modules`:
```
None                     → accept ALL from domains
[]                       → accept NOTHING
"bot-features"           → include (any domain)
"core:bot-features"      → include from specific domain
"admin:*"                → include ALL from domain
"-self-upgrade"          → exclude from any domain
"-admin:self-upgrade"    → exclude from specific domain
```

## Admin Workflow

### Register a Chat (ONE-STEP)

```
manage_chat(action="register",
            chat_key="telegram:-1001234567890",
            title="Team Chat",
            mode="active_plus",
            harness="teamwork")
```

This single command:
1. Registers the chat in the registry
2. Assigns the `teamwork` harness (model, domains, credentials)
3. Auto-assigns the `teamwork` RBAC role (from harness.default_role)
4. Domain config flows from the harness (write_domain=teamwork, refs derived)

### Inspect a Chat

```
manage_chat(action="get", chat_key="telegram:-1001234567890")
```

Shows: mode, harness, model, role, domains, write_domain (reference_domains derived at runtime).

### Change a Chat's Harness

```
manage_harness(action="assign", label="coding", chat_key="telegram:123456789")
```

Auto-assigns the harness's default_role via RBAC. Session resets for immediate effect.

### Create a Custom Harness

```
manage_harness(action="create", label="sales-team",
               fields='{"model": "claude-sonnet-4-6", "domains": ["teamwork", "sales"],
                        "write_domain": "teamwork", "default_role": "teamwork"}')
```

### Register a Domain

```
manage_domain(action="register", config_yaml="
domain:
  id: teamwork
  name: Teamwork Knowledge
  description: Team processes, business decisions, standards
  owner: telegram:123456789
  source:
    type: local
  knowledge:
    skills: [ms365-guide]
    facts_categories: [organization, operations]
")
```

### Set Up GitHub Repo with Deploy Key

```
manage_domain(action="setup_deploy_key", domain_id="teamwork")
```
Generates Ed25519 keypair, uploads to GitHub, stores encrypted.

### Sync Domain from GitHub

```
manage_domain(action="sync", domain_id="teamwork")
```
Clones repo via SSH deploy key, indexes docs into RAG, syncs skills + modules.

### Manage RBAC Roles

Roles are usually auto-assigned via harness.default_role. For manual overrides:

```
# Override a chat's role (persists even after harness reassignment)
manage_rbac(action="assign_role", subject_type="chat",
            subject_id="-1003969846997", role_id="bot_admin")
```

Admin-granted roles take priority over harness defaults.

## 6-Layer Knowledge Model

Each domain provides 6 layers:

| Layer | Description | Save | Pull | Storage |
|-------|------------|------|------|---------|
| INSTRUCTIONS | System prompt + CLAUDE.md + skills | Owner only | Auto-loaded | GitHub + FS |
| FACTS | Knowledge graph (triples) | FACTS_UPDATE (auto-routed) | Auto-injected | SQLite |
| KNOWLEDGE | Searchable docs + RAG | Index from GitHub | search_knowledge | SQLite RAG |
| FILES | Media, code, data | Write/Bash | Read/Glob | Filesystem |
| OPTIMIZATION | Self-improvement loop | Scheduled analysis | Owner reviews | SQLite |
| CREDENTIALS | OAuth, SSH keys, API keys | Admin stores | Auto-injected | SQLite (encrypted) |

## Data Flow

```
GitHub Repo ──sync──→ Bot (RAG + Skills + Modules)
                           │
User message ──→ System prompt (core + harness domains)
                           │
Model learns ──→ FACTS_UPDATE → auto-routed to harness.write_domain
                           │
Scheduled export ──→ Non-sensitive facts → GitHub /facts/
```

## Sensitivity Gate

Facts exported to GitHub are filtered:

**NEVER exported:** credentials, PII (emails, phones, IDs), doc_pointer refs,
JWT tokens, AWS keys, DB connection strings, high-entropy values
**EXPORTED:** process knowledge, decisions, technical standards, vendor info

## Backup Strategy

| Data | Backup | Recovery |
|------|--------|---------|
| Domain facts | Hourly 7z + GitHub export | Restore DB or import |
| RAG chunks | Hourly 7z | Re-index from GitHub |
| Media files | Hourly 7z | Re-fetch from Telegram |
| Credentials | Hourly 7z (encrypted) | Restore DB (needs Fernet key) |
| Skills/prompts | GitHub repo | Re-sync |
