# Changelog

All notable changes to this project will be documented in this file.

## [3.0.0] — 2026-05-20 — Domain Knowledge System (Sprint 17)

### 6-Layer Domain Knowledge Architecture
- **Canonical domains**: core (bot identity, all chats), admin (bot infra), teamwork (team knowledge)
- **Per-chat domain routing**: `write_domain` (ONE R/W) + `reference_domains` (derived at runtime from harness domains)
- **Flat permission model** (CB-79): DM = user subject, group/topic = chat subject. All users in a group share the chat's RBAC role.
- **Domain awareness prompt**: WRITE DOMAIN + REFERENCE DOMAINS + [WRITE]/[READ-ONLY]/[CORE] tags
- **Fact auto-routing**: FACTS_UPDATE auto-routes to write_domain. PII categories (health, finance, contact, documents, personal) stay chat-local.
- **500 pinned facts**: sorted by access_count DESC (tracks explicit searches only, not passive prompt load)

### New Modules
- `bot/storage/fact_export.py`: Bidirectional fact export to GitHub with sensitivity gate (PII, JWT, AWS, entropy detection)
- `bot/storage/fact_hygiene.py`: Staleness detection + dedup + auto-cleanup (protected: credentials, todo, documents)
- `bot/storage/domain_optimize.py`: Self-optimization via Gemini Flash analysis + recommendations table + owner approval
- `bot/storage/artifacts.py`: Coding artifact registry with FTS5 cross-session discovery

### New MCP Actions
- `manage_domain`: export_facts, run_hygiene, analyze, list_recommendations, review_recommendation
- `search_artifacts`: standalone tool for cross-session script/config discovery

### RBAC Changes
- New `teamwork` role: R/W teamwork domain + standard tools for group chats
- Removed `guest` and `domain_owner` roles (domain ownership via DomainConfig.owner field)
- Stale `harness_*` roles cleaned up at boot
- Teams UUID support: `_KEY_PATTERN` accepts non-numeric chat IDs, `is_private_chat()` accepts metadata

### Infrastructure
- SSH deploy keys for domain GitHub repos (Ed25519, /dev/shm tmpfs, scope=system)
- Domain scheduled tasks: hygiene (Sun 3am) + optimize (Sat 3am) per canonical domain
- `seed_canonical_domains()` at boot + `ensure_domain_scheduled_tasks()`
- Legacy `migrate_harness_to_rbac()` removed (170 lines dead code)

### Documentation
- New `docs/ADMIN_GUIDE.md`: entity model, 6-layer knowledge, domain lifecycle, admin operations
- `docs/ARCHITECTURE.md`: domain system section, 12 new module entries
- Architecture report: self-hosted HTML via `save_report` endpoint

## [2.2.0] — 2026-05-10 — Bot API Migration + WA Removal

### Sprint B4: Final Fixes + Docs
- **File handle exhaustion fix** (`telegram_botapi.py`): All media-sending functions (`send_photo`, `send_document`, `send_video`, `send_audio`, `send_voice`, `set_chat_photo`) now re-open files inside the retry lambda. Previously, if `_retry_on_429` fired a retry, the file handle was at EOF and the retry would send empty data.
- **`__all__` export list** (`telegram_botapi.py`): Added explicit `__all__` listing all 40+ public functions for clean wildcard imports. Includes underscore-prefixed names (`_split_message`, `_safe_filename`) that the userbot module also exports.
- **Documentation updated**: README (platform list, architecture diagram, license), CLAUDE.md (architecture tree, MCP tool count, TG factory pattern, removed WA references), DEPLOYMENT.md (Bot API setup section, forum topics, webhook secret marked REQUIRED), ACCESS_MODEL.md (platform list), CHANGELOG.md (this entry), `.env.example` (TG_WEBHOOK_SECRET marked REQUIRED, WA section commented out).

### Sprint B3: Forum Topics + Inline Keyboards
- **Forum topic MCP tools**: `create_forum_topic`, `close_forum_topic`, `reopen_forum_topic`, `delete_forum_topic` — 4 new MCP tools for managing supergroup forum topics.
- **Inline keyboard support**: `build_inline_keyboard()` helper + `reply_markup` param on `send_message`/`edit_message`. `answer_callback_query()` for button press handling. `parse_callback_query()` for webhook update parsing.
- **Bot menu commands**: `set_bot_commands()` and `delete_bot_commands()` for registering slash commands in the Telegram UI.

### Sprint B2: Bot API Core + Parser
- **Telegram Bot API integration** (`telegram_botapi.py`): Full Bot API backend using python-telegram-bot. Webhook-based, no polling. Same interface as userbot for transparent switching.
- **Bot API parser** (`tg_parser_botapi.py`): Webhook update parsing for Bot API mode.
- **Factory pattern** (`telegram.py`): `TG_MODE` env var selects backend (`"bot"` or `"userbot"`). All callers import from `integrations.telegram` — never reference backend modules directly.

### Sprint B1: WhatsApp Removal
- **WhatsApp bridge removed**: `whatsapp-bridge/` Go service, `whatsapp-mcp-server/`, `integrations/whatsapp.py`, and all WA MCP tools removed. Docker Compose no longer includes `wa-bridge` service.
- **WA volume mounts removed**: `wa-data`, `shared-media` volumes no longer needed.
- **Go bridge rules removed**: `.claude/rules/go-bridge.md` no longer applicable.

## [2.1.0] — 2026-05-10 — RBAC Release

### Sprint R5: MCP Tools + Cleanup + Docs
- **`manage_rbac` MCP tool** (`bot/mcp/rbac_tools.py`): Admin-only tool for runtime RBAC management. 10 actions: `list_roles`, `get_role`, `create_role`, `delete_role`, `add_policy`, `remove_policy`, `assign_role`, `revoke_role`, `check_access`, `audit`. Registered in bot-services MCP server. Admin-gated via both RBAC and legacy `_ADMIN_ONLY_TOOLS`.
- **Legacy hook cleanup**: Layers 1b (admin-only tool gate), 1b2 (harness allowed_tools), 1b3 (email allowlist), and 1c (external chat tool blocking) wrapped in `if not _RBAC_PRIMARY:`. These layers only execute when RBAC is disabled for rollback. Defense-in-depth layers always active: file path protection (L2), bash command protection (L3), bwrap sandbox (L3b), chat isolation (L1c.2), deploy gates (L1a+).
- **Legacy constants marked**: `_ADMIN_ONLY_TOOLS`, `_EXPLICIT_HARNESS_TOOLS`, `_EXTERNAL_CHAT_BLOCKED_PREFIXES`, `_EXTERNAL_CHAT_BLOCKED_EXACT`, `_EMAIL_GATED_PREFIXES` in `tool_labels.py` annotated as legacy (only used when `RBAC_PRIMARY=false`).
- **Documentation updated**: `ACCESS_MODEL.md` (RBAC as primary, harness is 12 behavior-only fields), `RBAC_DESIGN.md` (all 5 sprints marked DONE), `CLAUDE.md` (architecture section updated), `README.md` (RBAC feature description), `DEPLOYMENT.md` (`RBAC_PRIMARY` env var documented).

### Sprint R4: RBAC Primary
- **`RBAC_PRIMARY` feature flag** (`hooks.py`): When `true` (default), RBAC is the authoritative enforcement layer. Legacy hooks run log-only for mismatch detection. When `false`, legacy hooks are authoritative and RBAC is log-only (rollback mode).
- **Harness security fields removed**: `data_scope`, `allowed_tools`, `allowed_emails`, `knowledge_domains`, `sandbox`, `no_network` moved to RBAC policies. Harness reduced to 12 behavior-only fields.
- **Execution scope from RBAC**: `get_execution_scope()` on PolicyEngine determines filesystem (none/sandbox/full) and network (none/sandboxed/full) access. Injected into session state for bwrap configuration.
- **RBAC subject in session state**: `rbac_subject` (Subject dataclass) stored per-session. Built from user identity + team memberships + chat ID during access context construction.

### Sprint R3: Dual Enforcement
- **RBAC in PreToolUse hook**: Policy engine check runs alongside legacy layers. Both decisions logged for comparison. Mismatches logged as warnings.
- **RBAC in AccessContext**: `build_access_context()` queries RBAC for allowed domains and data scope.
- **RBAC in search_knowledge**: Domain access checked via RBAC policies.
- **Audit logging**: Every RBAC access decision (allow and deny) recorded in `rbac_access_log` table.

### Sprint R2: Role Seeding
- **Built-in roles**: `bot_admin` (admin on all), `employee` (use messaging + memory tools, read own chat data), `domain_admin` (admin on specific knowledge domains).
- **Auto-seed on startup**: Built-in roles and their policies created if absent.
- **Harness-to-RBAC mapping**: Security fields from existing harness configs translated to RBAC policies during seed.

### Sprint R1: Data Model
- **`bot/rbac/` package**: models.py (Subject, Policy, AccessDecision), schema.py (SQLite DDL), store.py (async CRUD), engine.py (PolicyEngine with in-memory cache, 5s TTL, deny-wins, default-deny).
- **SQLite tables**: `rbac_roles`, `rbac_policies`, `rbac_assignments`, `rbac_harness_roles`, `rbac_access_log`.
- **`is_domain_admin()`**: Migrated to RBAC engine with legacy domain YAML fallback.

## [2.0.0] — 2026-05-11 — Corporate Platform Release

### Sprint 16: Graph API Document Sources
- **Microsoft Graph API sync** (`bot/storage/graph_sync.py`): Index documents from SharePoint sites, Teams channel files, and OneDrive for Business. Parallel to `github_sync.py`, reuses the same `doc_parser` + `index_document` pipeline.
- **SSRF prevention**: `_is_safe_graph_url()` validates all follow-up URLs point to `graph.microsoft.com`.
- **Delta query support**: Incremental sync via Graph API delta endpoints for efficient updates.
- **Domain source type**: `DomainSource.type` now supports `graph_api` alongside `github` and `local`.
- **New example configs**: `examples/domains/sharepoint-docs.yaml` and `examples/domains/teams-channel-docs.yaml`.
- **Required permissions**: `Files.Read.All` and `Sites.Read.All` (delegated or application).

### Sprint 15: Gemini Document Intelligence
- **Gemini-powered document parsing** (`bot/storage/doc_parser.py`): PDF, DOCX, XLSX, PPTX, and images parsed via Gemini Flash multimodal. Structured text extraction preserves headings, tables, and code blocks. Fallback to legacy parsers (pypdf, python-docx) when Gemini is unavailable.
- **Contextual chunk enrichment** (`bot/storage/domains.py`): Anthropic Contextual Retrieval pattern -- Gemini Flash generates a one-line context prefix for each chunk at indexing time, situating it within its source document. Improves retrieval quality by ~49%.
- **Configurable Gemini model**: `GEMINI_MODEL_DESCRIBE` env var (default: `gemini-2.5-flash-lite`).
- **Async document parsing**: `parse_document_async()` and `parse_file_content_async()` for use in async indexing pipelines.

### Sprint 14: Dynamic Context Filling
- **Two-tier knowledge architecture**: Hot context (always in prompt) + cold context (on-demand RAG search).
- **Knowledge map generation** (`bot/storage/domain_summary.py`): Gemini Flash generates compressed knowledge maps (TOPICS / KEY INFORMATION / DIVE DEEPER format) for each domain. Cached in `domain_registry.hot_summary`.
- **Hot summary injection**: Domain summaries injected into system prompt via `build_domain_awareness_prompt()`. Model has immediate domain awareness without search tool calls.
- **Essential documents config**: `knowledge.essential_docs` and `knowledge.hot_context_budget` in domain YAML.
- **Fallback summary**: File-list summary (document inventory + key facts) when Gemini is unavailable.
- **Cache refresh**: Auto-regenerated on domain re-sync, manual via `manage_domain(action="refresh_summary")`.

### Sprint 13: Documentation Overhaul
- **DEPLOYMENT.md**: Complete admin deployment guide -- Azure AD setup, env vars reference, knowledge domains, harnesses, backup, monitoring.
- **DOMAIN_COOKBOOK.md**: 8-section practical recipe book with engineering, HR, sales, multi-domain, fact routing, search optimization, access patterns, and health monitoring examples.
- **ARCHITECTURE_DIAGRAM.md**: 4 Mermaid diagrams -- system architecture, knowledge domain data flow, permission model, message pipeline.
- **KNOWLEDGE_DOMAINS_ARCHITECTURE.md**: 9-layer domain stack design with composition flow and migration plan.
- **DYNAMIC_CONTEXT_ARCHITECTURE.md**: Two-tier knowledge architecture design with phase plan.
- **ACCESS_MANAGEMENT.md**: Full 4-tier permission model with credential tiers.
- **CONTRIBUTING.md**: Upstream/downstream fork model, code standards, CI pipeline.
- **README.md**: Comprehensive project overview with all features, architecture, quick start, and documentation index.

### Sprint 12: Testing & CI/CD
- **CI pipeline**: GitHub Actions workflow with Python linting (ruff), syntax verification, and test execution.
- **Staging QA**: `staging_qa_run` tool with regression scenarios, parallel execution, per-scenario isolation.
- **Smoke tests**: Import verification and basic handler tests.
- **pyproject.toml**: Ruff configuration for consistent code style.

### Sprint 11: Security & Compliance
- **Encryption documentation** (`docs/ENCRYPTION.md`): Current encryption state (Fernet vault, backup AES-256) + options for full encryption at rest (LUKS, SQLCipher).
- **Key rotation procedures** (`docs/KEY_ROTATION.md`): Rotation procedures for all 7 secret types (Claude OAuth, Azure AD, GitHub PAT, Fernet key, Gemini API, backup password, TOTP).
- **Token vault hardening**: Fernet key validation on startup, encrypted token cleanup on user deletion.

### Sprint 10: Corporate Reframing
- **Desktop Relay -> Employee Workstation Control**: All skills, prompts, docs, and code comments reframed. "Relay chats" -> "workstation channels", "Mac relay" -> "employee Mac workstation". Every employee gets a managed channel to their own Mac, scoped to their machine + data only.
- **WhatsApp -> WhatsApp Business Channel**: Skills and prompts updated for B2C/B2B support context. Third-party correspondence reframed for vendor/client/partner communications.
- **Phone Calls -> Voice Assistant & Helpdesk**: Phone call skill reframed for corporate helpdesk use cases (vendor inquiries, client follow-ups, service provider calls).
- **Travel Search -> Travel & Expense Management**: Travel skill reframed for corporate T&E workflows.
- **Generic skills** (image, diagram, docx, pptx, html-report): Descriptions updated to explicitly reference corporate use cases.
- **Skills catalog module**: All 17 skill descriptions updated with corporate language.
- **All relay Python module docstrings** (bot/relay/*.py, bot/desktop_relay.py): Reframed for employee workstation control.
- **Docs** (DESKTOP_RELAY_SETUP.md, CLAUDE_RELAY.md, RELAY_OPERATOR_GUIDE.md, components/relay.md, ARCHITECTURE.md): Titles and descriptions updated.

### Sprint 9: Knowledge Domains Architecture
- **9-layer knowledge stack**: Per-domain prompt modules, skills, auto-load skills, and fact categories with multi-domain composition.
- **Domain awareness prompt**: Active domain descriptions injected into system prompt for AI routing.
- **Domain-scoped facts**: `domain_id` column on `message_facts` table with model-driven write routing.
- **Composition helpers**: `compose_effective_modules()`, `compose_domain_categories()` in domains.py. (Legacy `compose_domain_modules()` removed in CB-119.)

### Sprint 8: External Chat Rename
- All references to "discovered chat" reframed to "external chat" in corporate context.

## [1.2.0] — 2026-05-05

### Added
- **Harness system**: Per-chat configuration profiles (18 fields: model, effort, turns, budget, tools, prompts, project dirs, sandbox). 4 canonical harnesses auto-created at startup (admin, team, external_chat, coding)
- **PM Agent Bus**: Structured task execution with 20 MCP tools, SQLite DB, ticket/sprint/project tracking, budget cascade, rolling sprints, retrospectives
- **Desktop relay**: Mac Claude Code sessions controlled via Telegram groups, persistent SDK daemons, SSH-over-Tailscale, real-time Desktop→TG forwarding, TOTP auth, Mac install approval gate
- **Modular architecture refactor**: Main codebase decomposed into bot/core/, bot/mcp/ (7 modules), bot/relay/ (6 modules), bot/pm/ (8 modules), bot/handlers, bot/dispatch, bot/commands, bot/streaming
- **Document creation skills**: PPTX, DOCX, diagrams (Mermaid), HTML reports with Chart.js + Tailwind
- **Sandbox networking**: Unix socket proxy bridge for coding sandboxes (bwrap + proxy allows HTTP/HTTPS while blocking internal services + private IPs)
- **External chat isolation**: Admin suppression in external chats, chat_id enforcement on send tools, per-chat tool overrides with runtime management
- **Cost optimization**: Default Sonnet model, per-chat model switching, session USD budgets ($5/$50), per-query output token caps (25k/50k), rate limit monitoring with alerts
- **Usage tracking**: SQLite usage_log table, per-query cost logging, usage_report tool with multi-timeframe breakdowns
- **Google OAuth pre-refresh**: Proactive token refresh before expiry (prevents transient auth errors)
- **Relay burst debounce**: 150ms coalescing for rapid-fire messages to relay daemons
- **PDF text extraction**: Full text via Gemini Flash with dedicated extraction prompt
- **File-send path validation**: Per-tier allowlists with realpath traversal protection
- **Read size guard**: Prevents SDK 1MB JSON buffer crash on large file reads
- **Deep health check** (`/health/deep`): Exercises harness resolution pre-deploy, catches NameErrors
- **Pyflakes Gate 5**: Static analysis in deploy pipeline catches undefined names

### Security
- **4-tier access control**: Admin / Non-admin team / Coding sandbox / External chat
- **8 enforcement layers**: Container isolation, user auth, admin suppression (Layer 1a), admin tool gating (Layer 1b), harness whitelist (Layer 1b2), external chat blocking (Layer 1c), chat isolation (Layer 1c.2), file/bash path protection (Layer 2/3), bubblewrap kernel sandbox (Layer 3b)
- **Host watcher isolation**: Sensitive-file gate with manual approval for infra changes
- **Guard.sh hardening**: Relay-* only commands, no eval, path allowlists, install approval gate
- **Sandbox hard-deny**: bwrap unavailability blocks Bash entirely (no regex-only fallback)
- **Per-chat data isolation**: AccessContext scopes all queries (messages, facts, media, RAG)

### Infrastructure
- Docker container (Python 3.12 + Node.js 22 + Playwright Chromium)
- SQLite storage with WAL mode (messages, facts, media cache, PM data)
- Persistent Docker volumes for all state
- Staging environment with isolated volumes, parallel QA, commit verification
- Automated backup (hourly, encrypted 7z, Mac + GDrive destinations)
- Host watcher with auto-rollback + failed deploy log capture
- 15 test files (smoke, integration, staging, data isolation)
- AGPL-3.0 license (whatsmeow/libsignal dependency)
