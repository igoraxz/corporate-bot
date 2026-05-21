# Corporate Bot

**AI-powered corporate assistant for MS Teams + Telegram Bot API**

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Claude Agent SDK](https://img.shields.io/badge/Claude-Agent%20SDK-blueviolet)](https://docs.anthropic.com/en/docs/agents/claude-agent-sdk)
[![MS Teams](https://img.shields.io/badge/MS%20Teams-Bot-6264A7.svg)](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4.svg)](https://core.telegram.org/bots/api)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg)](https://docs.docker.com/compose/)

Deploy an AI assistant inside your organization's Microsoft Teams. Corporate Bot connects to your documentation repositories, respects your permission boundaries, and gives every employee a personal AI assistant scoped to the knowledge they need -- all running within your corporate perimeter.

Built on the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/claude-agent-sdk). Forked from [corporate-bot](https://github.com/igoraxz/corporate-bot) (OSS). All company-specific data lives in configuration files and knowledge domains, not in source code.

---

## Key Features

### Knowledge Domains
Composable knowledge bases backed by GitHub repositories and Microsoft Graph API (SharePoint, Teams channel files, OneDrive). The bot automatically indexes, chunks, embeds, and serves your documentation. Content-hash gating means incremental syncs cost 80-95% less in embeddings. Each Teams channel sees only its assigned domains. Gemini-powered document processing handles PDF, DOCX, XLSX, PPTX, and images natively. Contextual chunk enrichment (Anthropic Contextual Retrieval pattern) improves retrieval quality by prepending document-level context to each chunk at indexing time.

### MS Teams Integration
Native Teams app with Adaptive Cards, SSO, and proactive messaging. Employees interact through DMs and channel @-mentions. Zero-click authentication via Teams On-Behalf-Of token exchange gives each employee access to their own Outlook inbox and calendar in DMs.

### Multi-Tenant Harness System
Per-channel configuration profiles control model selection, tool access, domain visibility, and budget caps. Map Teams channels to harnesses automatically via rules or manually via admin commands. Each team gets exactly the capabilities they need.

### RAG-Powered Search
Semantic search across indexed documentation using Gemini embeddings (3072-dim vectors) with cosine similarity ranking. Domain-scoped queries ensure employees only see results from their authorized knowledge bases. Supports Markdown, PDF, DOCX, XLSX, PPTX, images, and code files. Two-tier knowledge architecture: hot context (Gemini-generated knowledge maps injected into the system prompt for immediate domain awareness) and cold context (full RAG search on demand).

### Per-User MS365 SSO
Teams SSO with On-Behalf-Of token exchange provides zero-click authentication. Per-user OAuth tokens are stored in a Fernet-encrypted vault. In DMs, each employee accesses their own email and calendar -- never another employee's data.

### Employee Workstation Control
Bridge dedicated Telegram channels to employee Mac Claude Code sessions via SSH over Tailscale. Persistent SDK daemons with real-time bidirectional streaming, file attachments, and catchup digests on reconnection. Each employee gets a managed channel scoped to their own machine.

### RBAC (Role-Based Access Control)
Sole authorization layer for all tool access. Two built-in roles: Bot Admin (full access) and Employee (scoped tools + data). Domain Admin is a custom role for knowledge base management. Custom roles supported with granular policies on 6 object types (knowledge domains, credentials, tools, chat data, filesystem, network). Deny-wins resolution with default-deny. Admin MCP tool (`manage_rbac`) for runtime role/policy/assignment management. Full audit log of every access decision. Bootstrap admin via `ADMIN_USERS` env var (auto-assigns `bot_admin` on first boot). Defense-in-depth: file path protection, bash command checks, bubblewrap kernel sandbox, and chat isolation run alongside RBAC.

### Employee Onboarding Wizard
Interactive setup flow for new team members. The bot detects first-time users, auto-assigns the employee RBAC role, and guides them through Claude OAuth, M365/Google Workspace auth, knowledge domain access requests, and profile setup. Admins monitor progress via `/onboarding` dashboard. Steps are resumable and skippable — employees can start using the bot immediately and complete optional setup later.

### Audit Logging
Per-user, per-team activity tracking with cost attribution. CSV export for compliance reviews. GDPR-compliant data deletion by Azure AD Object ID. Every tool call, domain access, and cost metric is recorded.

### Messaging Channels
Telegram Bot API (webhook-based with inline keyboards, forum topics, and menu commands) provides the admin control channel alongside Teams. Supports both Bot API mode (corporate, default) and Pyrogram MTProto userbot mode (personal/family) via the `TG_MODE` config switch. Voice assistant via Vapi for outbound AI phone calls.

### CI/CD Ready
Self-upgrade from chat: the bot edits its own code, runs security and quality review agents, commits, pushes, and deploys -- with automated syntax checks, staging QA, and auto-rollback on failure. Host-side deploy watcher with sensitive-file gates for infrastructure changes.

---

## Deployment Modes

Choose your messaging platform — or use both simultaneously:

| Mode | Platform | Auth | Best For |
|------|----------|------|----------|
| **Teams-only** | Microsoft Teams | Azure AD SSO | Corporate environments with M365 |
| **Telegram-only** | Telegram Bot API | Bot token + webhook | Dev teams, startups, personal use |
| **Dual-platform** | Both simultaneously | Both | Maximum flexibility |

Each mode is fully supported with platform-specific UX (streaming, rich formatting, file sharing).

---

## Quick Start

```bash
# 1. Clone your organization's fork
git clone git@github.com:your-org/corporate-bot.git && cd corporate-bot

# 2. Configure environment
cp .env.example .env
# Edit .env — required for ALL modes:
#   CLAUDE_CODE_OAUTH_TOKEN    — Claude AI subscription token
#   ROOT_ADMIN                 — Your admin identity (teams:aad-id or telegram:user-id)
#   ADMIN_USERS                — Same format as ROOT_ADMIN
#   TOKEN_ENCRYPTION_KEY       — Generate: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   GEMINI_API_KEY             — RAG search + media descriptions (free: https://aistudio.google.com/apikey)

# Edit .env — choose platform (at least one):
#   Teams-only:    TEAMS_ENABLED=true, TEAMS_APP_ID, TEAMS_APP_SECRET, TEAMS_TENANT_ID
#   Telegram-only: TG_BOT_TOKEN, TG_WEBHOOK_URL, TG_WEBHOOK_SECRET
#   Dual-platform: Set both groups

# 3. Start the bot
docker compose up -d

# 4. (Teams) Upload app manifest from teams-app/ directory
#    (Telegram) Webhook auto-registers on boot
# 5. Verify
curl http://localhost:8000/health
```

**Detailed guides:** [Admin Handbook](docs/ADMIN_HANDBOOK.md) (complete setup walkthrough) | [Deployment Guide](docs/DEPLOYMENT.md) (infrastructure details)

---

## Architecture

```
MS Teams / Telegram Bot API
         |
         v
+---------------------------+
|   FastAPI (main.py)       |    Webhook-based (Teams + TG Bot API)
|   Task Queue (SQLite)     |    Crash-safe, burst coalescing
+---------------------------+
         |
+---------------------------+
|   Client Pool             |    One SDK client per chat
|   Security Hooks          |    Flat RBAC, 7 enforcement layers
|   Harness System          |    Per-channel config profiles
+---------------------------+
         |
+---------------------------+
|   Claude Agent SDK        |    Sonnet / Opus, persistent sessions
|   84 MCP Tools            |    Messaging, memory, search, deploy, PM, domains
|   18 On-Demand Skills     |    Loaded by SDK when relevant
+---------------------------+
         |
+-----+-----+-----+-----+-----+-----+
|     |     |     |     |     |     |
v     v     v     v     v     v     v
SQLite  GitHub  Graph   Gemini  Azure  Playwright
(RAG,   (Domain API     (Embed, AD     Camoufox
 Facts,  Sync)  (SP/    Media,  (SSO)  (Browser)
 Audit)        Teams)  DocAI)
```

For detailed visual diagrams (Mermaid), see [docs/ARCHITECTURE_DIAGRAM.md](docs/ARCHITECTURE_DIAGRAM.md).
For the full component reference, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Configuration

### Environment Variables

All configuration is in `.env` (secrets) and `data/org_config.json` (organization identity). See [`.env.example`](.env.example) for the complete reference with descriptions for every variable.

### Knowledge Domains

Create YAML configs in `data/domains/`, register via the `manage_domain` MCP tool, and set up GitHub webhooks for automatic sync. Domains can source documents from GitHub repositories, Microsoft Graph API (SharePoint sites, Teams channel files), or local filesystems. See [docs/DOMAIN_COOKBOOK.md](docs/DOMAIN_COOKBOOK.md) for practical examples covering engineering, HR, sales, SharePoint, Teams channels, and multi-domain setups.

### Harnesses

Per-channel configuration profiles that control model, tools, domain access, and budget. Managed via `manage_harness` MCP tool and `/set-harness` slash command. Canonical harnesses: `admin`, `coding`, `teamwork`, `personal`, `external_chat`, `project-coding`.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for harness creation examples.

---

## Security

| Layer | Protection |
|-------|-----------|
| Container | Docker, non-root `botuser`, all capabilities dropped |
| Authentication | Azure AD JWT validation, AAD Object ID tier resolution |
| Tool gating | RBAC tool policies |
| Email allowlist | Per-harness `allowed_credentials` for shared inbox access |
| Domain scoping | `allowed_domains` restricts knowledge search |
| Chat isolation | `allowed_chat_ids` prevents cross-chat data leakage |
| Kernel sandbox | Bubblewrap namespace isolation for code execution |
| Token vault | Fernet-encrypted per-user OAuth tokens |

See [docs/ACCESS_MANAGEMENT.md](docs/ACCESS_MANAGEMENT.md) for the full permission model.
See [docs/ENCRYPTION.md](docs/ENCRYPTION.md) for encryption at rest options.
See [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md) for secret rotation procedures.

---

## Documentation

| Document | Purpose |
|----------|---------|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Step-by-step admin deployment guide |
| [docs/DOMAIN_COOKBOOK.md](docs/DOMAIN_COOKBOOK.md) | Knowledge domain configuration recipes |
| [docs/ARCHITECTURE_DIAGRAM.md](docs/ARCHITECTURE_DIAGRAM.md) | Visual Mermaid architecture diagrams |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component reference and tuning parameters |
| [docs/ACCESS_MANAGEMENT.md](docs/ACCESS_MANAGEMENT.md) | Flat RBAC permission model and credential tiers |
| [docs/ENCRYPTION.md](docs/ENCRYPTION.md) | Encryption at rest options |
| [docs/KEY_ROTATION.md](docs/KEY_ROTATION.md) | Secret rotation procedures |
| [docs/KNOWLEDGE_DOMAINS_ARCHITECTURE.md](docs/KNOWLEDGE_DOMAINS_ARCHITECTURE.md) | 9-layer domain stack architecture |
| [docs/DYNAMIC_CONTEXT_ARCHITECTURE.md](docs/DYNAMIC_CONTEXT_ARCHITECTURE.md) | Two-tier knowledge architecture (hot/cold context) |
| [docs/CORPORATE_ARCHITECTURE.md](docs/CORPORATE_ARCHITECTURE.md) | Corporate module additions |
| [docs/DESKTOP_RELAY_SETUP.md](docs/DESKTOP_RELAY_SETUP.md) | Employee workstation relay setup |
| [docs/HOST_SECURITY.md](docs/HOST_SECURITY.md) | Host watcher isolation and hardening |
| [CLAUDE.md](CLAUDE.md) | Full developer guide (architecture, coding rules, pitfalls) |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines and upstream/downstream model |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

---

## Development

### Prerequisites

- Docker Engine 24+ with Compose v2
- Python 3.12+
- Node.js 22+ (for MCP servers)

### Testing

```bash
# Unit tests
python -m pytest tests/ -v -k "not Live"

# Staging integration tests
python -m pytest tests/test_staging.py -v
```

### Deploy Process

1. Edit code, verify syntax
2. Run security and quality review agents
3. `git add` + `git commit` + `git push`
4. `deploy_review` -- syntax, imports, smoke tests, pyflakes, staging boot
5. `staging_qa_run` -- regression scenarios against staging
6. `deploy_bot(action="rebuild")` -- triggers host watcher
7. Auto-rollback on health check failure

See [CLAUDE.md](CLAUDE.md) for the full deploy gate sequence.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the upstream/downstream fork model, code standards, and contribution guidelines.

---

## License

AGPL-3.0 -- see [LICENSE](LICENSE).

If you modify and deploy this software as a network service, you must make the complete source code available to users of that service under AGPL-3.0 terms.
