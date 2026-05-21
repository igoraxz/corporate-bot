# Corporate Bot — Architecture & Tuning Reference

For setup instructions see [README.md](../README.md). For developer guide see [CLAUDE.md](../CLAUDE.md).

## Component Knowledge Files

Each major subsystem has a compact, loadable knowledge file — read the relevant one(s) BEFORE
modifying that component. These define features, design principles, anti-patterns, pitfalls, and
implementation schema for each subsystem.

| Component | File | What it covers |
|-----------|------|---------------|
| Agent / SDK Sessions | [components/agent.md](components/agent.md) | ClaudeSDKClient lifecycle, budget, rate limits, session isolation, hook registration |
| Security / Hooks | [components/hooks.md](components/hooks.md) | RBAC-enforced access model, 7 enforcement layers, blocklists, session state resolution |
| Harness System | [components/harness.md](components/harness.md) | Per-chat config profiles, domain composition (write_domain + refs), sandbox |
| Memory / RAG / Facts | [components/memory.md](components/memory.md) | SQLite DB, AccessContext isolation, facts graph, RAG chunking, search tools |
| Employee Workstation Control | [components/relay.md](components/relay.md) | SSH transport, daemon pool, TOTP auth, catchup scenarios, injection semantics |
| PM Agent Bus | [components/pm.md](components/pm.md) | In-session v5 execution, TCD checkpoints, ticket protocol, budget cascade |
| MCP Tool Servers | [components/mcp.md](components/mcp.md) | Per-session isolation, size guard, server map, external MCP configs |
| Media Handling | [components/media.md](components/media.md) | Auto-descriptions, voice transcription, tmp/ vs media_cache/, TG re-fetch |
| Task Queue | [components/task_queue.md](components/task_queue.md) | PersistentQueue, burst coalescing, mid-task injection, streaming UX |

**Usage**: Load the relevant component file at Stage SM (Subject Matter Research) before any coding
task touching that subsystem. Files are compact (~200-400 lines) and designed for quick context loading.

---

## Module Responsibilities

| Module | Responsibility | Key Config |
|--------|---------------|------------|
| `main.py` | HTTP server, TG Pyrogram userbot, WA polling, scheduler loop | HOST, PORT, TG_CHAT_ID |
| `config.py` | Load org_config.json + env vars, expose all config constants, re-exports from config_overrides | DATA_DIR, all *_API_KEY |
| `config_overrides.py` | Per-chat model/OAuth/effort overrides, OAuth profile CRUD, backed by JsonConfigStore | OAUTH_DEFAULT_LABEL |
| `bot/config_store.py` | Unified JSON config file class: atomic writes (tmp+rename), threading.Lock, mtime-based reload, key type coercion | — |
| `bot/agent.py` | SDK client lifecycle, persistent sessions (SQLite session_map), query execution | MAX_TURNS |
| `bot/hooks.py` | Security: admin suppression (Layer 1a), admin gating (Layer 1b), per-harness tool gating (Layer 1c), chat isolation (Layer 1c.2), bash blocklist, per-client hook closures for session isolation | ADMIN_USERS |
| `bot/mcp_tools.py` | Custom MCP tools (45+ tools in 3 servers), fact management, deploy diagnostics | TOOL_TIMEOUT |
| `bot/mcp_config.py` | External MCP server configs (Playwright, Camoufox, Google, MS365/Softeria, Kiwi Flights) | CAMOFOX_BROWSER_URL, KIWI_MCP_URL |
| `bot/mcp_size_guard.py` | MCP proxy: response truncation, image-to-file, viewport injection | MCP_MAX_RESPONSE_BYTES |
| `bot/storage/memory.py` | SQLite storage: messages, message_facts, media, AccessContext for per-chat data isolation  | DB_PATH |
| `bot/storage/rag.py` | Semantic search: per-chat isolated chunks, Gemini embeddings, AccessContext-scoped queries  | RAG_* |
| `bot/scheduler.py` | Scheduled tasks: morning brief, email check, self-optimize | TASKS_FILE |
| `bot/pipeline/task_queue.py` | PersistentQueue: SQLite-backed task queue, crash-safe dispatch, catch-up debounce  | MAX_PARALLEL_TASKS |
| `bot/prompts.py` | System prompt builder: loads data/prompts/*.txt, fills placeholders | PROMPTS_DIR |
| `integrations/telegram.py` | TG Pyrogram MTProto userbot, media metadata extraction, path traversal protection | TG_API_ID, TG_API_HASH |
| `integrations/teams.py` | MS Teams Bot Framework integration | TEAMS_APP_ID |
| `integrations/phone.py` | Vapi voice assistant & helpdesk client | VAPI_API_KEY |
| `bot/storage/media.py` | Background media description orchestration  | MEDIA_DESCRIBE_MAX_CONCURRENT |
| `bot/storage/domains.py` | Knowledge domains: registry, indexing, RAG chunks, search, prompt composition, canonical domain seed | — |
| `bot/storage/github_sync.py` | GitHub repo sync: HTTPS API + SSH deploy key clone, webhook + polling, skill/module sync | GITHUB_TOKEN |
| `bot/storage/fact_export.py` | Bidirectional fact export: sensitivity gate, push via SSH/API | — |
| `bot/storage/fact_hygiene.py` | Fact staleness detection, deduplication, auto-cleanup | — |
| `bot/storage/domain_optimize.py` | Self-optimization: Gemini analysis of chat histories, recommendations table, owner approval | GEMINI_API_KEY |
| `bot/storage/artifacts.py` | Coding artifact registry: FTS5 search, cross-session discovery | — |
| `bot/core/access.py` | Data isolation: AccessContext, flat permission model (DM=user, group=chat), SQL filters | — |
| `bot/core/fact_persistence.py` | FACTS_UPDATE parsing, domain auto-routing, RBAC write check, admin flagging | — |
| `bot/chat_registry.py` | Chat/topic registration: mode, topic inheritance (domain config from harness) | — |
| `bot/rbac/seed.py` | RBAC role seeding: bot_admin, employee, teamwork, external + migration | — |

## Domain Knowledge System (6-Layer Model)

Each domain provides 6 layers of knowledge:

| Layer | Storage | Access Pattern |
|-------|---------|---------------|
| INSTRUCTIONS | GitHub repo + composed FS artifacts | Auto-loaded into system prompt |
| FACTS | SQLite `message_facts` (domain_id column) | Auto-injected into volatile header |
| KNOWLEDGE | SQLite `rag_chunks` + PM tables | On-demand via `search_knowledge` |
| FILES | Filesystem (media, code, reports) | Via Read/Write/Bash tools |
| OPTIMIZATION | SQLite `domain_recommendations` | Scheduled analysis + owner approval |
| CREDENTIALS | SQLite `credential_store` (Fernet encrypted) | Auto-injected into tool auth |

**Canonical domains:** `core` (bot identity, all chats), `admin` (bot infra), `teamwork` (team knowledge).

**Per-chat config via harness:** `domains` (active) + `write_domain` (R/W) + `default_role` (RBAC).
Reference domains derived: `domains - write_domain - core`.

**Flat permissions:** DM = user subject, group/topic = chat subject. All users in a group share the chat's RBAC role.

**RBAC roles:** `bot_admin` (full), `teamwork` (R/W teamwork domain), `employee` (read-only), `external` (restricted for 3rd-party chats). Domain ownership via DomainConfig.owner field.
| `integrations/gemini.py` | Gemini API: image gen/edit + media descriptions | GEMINI_API_KEY |
| SDK built-in: WebSearch, WebFetch | Web search and fetch (provided by Claude SDK) | — |

## Tunable Parameters

### RAG (config.py → bot/storage/rag.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| RAG_CHUNK_SIZE | 7 | Max messages per chunk (cap) |
| RAG_MAX_CHUNK_CHARS | 2000 | Character budget per chunk (primary sizing control) |
| RAG_CHUNK_OVERLAP_MSGS | 2 | Overlap N messages between consecutive chunks |
| RAG_LONG_MSG_OVERLAP_CHARS | 200 | Character overlap when splitting long messages into sub-chunks |
| SIMILARITY_THRESHOLD | 0.25 | Minimum cosine similarity for results |
| RAG_EMBEDDING_MODEL | gemini-embedding-001 | Embedding model (3072-dim) |
| RAG_EMBEDDING_BATCH_SIZE | 100 | Texts per embedding batch |

### Agent (config.py → bot/agent.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| MAX_TURNS | 75 | Max tool-call iterations per query |
| SESSION_TIMEOUT | 31536000 (365d) | SDK client keepalive duration (effectively permanent) |
| FORK_SESSION_TIMEOUT | 3600 (1h) | Hard kill timeout for forked task sessions |
| SDK_CONNECT_RETRIES | 2 | Retry count for SDK connect() failures |
| SDK_CONNECT_BACKOFF_BASE | 3.0 | Backoff base (seconds, doubles each retry) |
| SDK_INIT_TIMEOUT | 180 | SDK init timeout (seconds) |
| MAX_TOOL_RESULT_CHARS | 30000 | Truncation limit for tool outputs |
| TOOL_TIMEOUT | 90 | Per-tool execution timeout (seconds) |

### Streaming (main.py, config.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| STREAM_EDIT_INTERVAL | 3.0s | Min interval between TG message edits |
| LONG_STATUS_INTERVAL | 150s | WA per-task status update interval (2.5 min) |
| LONG_SOFT_TIMEOUT | 600s | Soft timeout for long tasks |
| MAX_PARALLEL_TASKS | 3 | Max concurrent tasks per chat |
| MAX_TOTAL_TASKS | 15 | Global cap across all chats |

### Memory (bot/storage/memory.py, config.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| MEDIA_RETENTION_DAYS | 36135 | Days to keep cached media (99 years — effectively permanent) |
| MAX_MEDIA_SIZE_MB | 20 | Max file size for media cache |
| MEDIA_DESCRIPTION_ENABLED | true | Auto-generate AI descriptions for all media types |
| MEDIA_DESCRIPTION_BACKFILL_LIMIT | 20 | Max undescribed media to process on startup |
| MEDIA_DESCRIBE_MAX_SIZE_MB | 100 | Max file size for AI description (files larger are skipped) |
| MEDIA_DESCRIBE_INLINE_SIZE_MB | 20 | Files under this sent inline; larger use Gemini File API |
| MEDIA_DESCRIBE_TIMEOUT_S | 120 | Timeout per Gemini description call |
| MEDIA_DESCRIBE_MAX_CONCURRENT | 3 | Max concurrent Gemini description calls (semaphore) |

### Scheduler (bot/scheduler.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| Check interval | 30s | How often scheduler checks for due tasks |
| Default tasks | morning, email_check, evening, selfopt | Seeded if none exist |

Task times are configurable via the `manage_scheduled_task` MCP tool — no env vars needed.

### Browser (bot/mcp_config.py, bot/mcp_size_guard.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| PLAYWRIGHT_MCP_PORT | 3001 | Playwright shared SSE server port |
| CAMOFOX_BROWSER_URL | http://camofox-browser:9377 | Camoufox REST API URL |
| CAMOFOX_API_KEY | (none) | Shared secret for camofox-browser auth |
| MCP_MAX_RESPONSE_BYTES | 500KB | Size guard truncation threshold |
| MCP_IMAGE_FILE_PREFIX | "screenshot" | Screenshot filename prefix (Camoufox overrides to "camofox") |
| CAMOFOX_MAX_TABS | 3 | Max concurrent browser tabs |
| CAMOFOX_SESSION_TIMEOUT | 300000 | Tab idle timeout (ms) |

## Browser Architecture

Two browser engines for different site types:

| | Playwright | Camoufox |
|---|---|---|
| **Engine** | Chromium (headless) | Firefox (anti-detect) |
| **Stealth** | Basic | C++ fingerprint spoofing (undetectable by CreepJS, DataDome, Cloudflare) |
| **Use for** | General browsing, portals, forms | Airlines, flight aggregators, anti-bot sites |
| **Runs as** | Shared SSE server (port 3001) in bot-core | Separate Docker container (port 9377) |
| **Tab model** | One tab per SDK client (shared browser) | Configurable max tabs (default 3) |
| **MCP tools** | `browser_*` (navigate, snapshot, click, type) | `camofox_*` (navigate, snapshot, click, scroll) |

### MCP Size Guard (`bot/mcp_size_guard.py`)

All browser MCP servers are wrapped with the size guard proxy:

- **Text truncation**: Responses >500KB truncated to prevent SDK 1MB JSON buffer crash
- **Image-to-file**: All base64 screenshots saved to `data/tmp/<prefix>-<timestamp>.png`
- **Request injection**: Auto-injects viewport 1920x1080 + light colorScheme into `create_tab` calls (Camoufox defaults to 1280x720 dark)
- **Chunked pipe reads**: 64KB reads prevent stdio deadlocks on heavy pages
- **Two modes**: `wrap` for stdio servers (Camoufox), `sse` for SSE→stdio bridging (Playwright)

### Fallback Pattern

1. Try Playwright first (faster, lower resource usage)
2. If site blocks/CAPTCHAs Playwright → switch to Camoufox
3. Known anti-bot sites (airlines, flight aggregators) → go directly to Camoufox

## Memory System

The memory system is a multi-layered architecture in SQLite (`conversations.db`) combining full-text search, semantic search (RAG), structured facts, and media caching with AI-generated descriptions.

### Database Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `messages` | All conversation messages | id, source, user_name, user_id, text, role, timestamp, msg_type, media_cache_id |
| `message_facts` | Structured facts (graph triples) | id, subject, fact_key, fact_value, category, source_msg_id, source_chunk_id, doc_pointer, superseded_by, expired |
| `rag_chunks` | RAG embedding chunks | chunk_id, start_msg_id, end_msg_id, chunk_text, embedding (float32 blob, 3072-dim), senders, timestamps |
| `media_cache` | Cached media files | id, filename, media_type, source, description, file_path, tg_file_id, cached_at, expires_at |
| `session_map` | SDK session persistence across restarts | session_key, session_id, last_activity |
| `messages_fts` | FTS5 index on messages | text, user_name |
| `message_facts_fts` | FTS5 index on facts | subject, fact_key, fact_value, category |
| `media_cache_fts` | FTS5 index on media | filename, original_filename, description, sender_name, media_type |

### Structured Facts (message_facts)

Facts are graph triples (subject/key/value) with metadata, extracted from conversations via `FACTS_UPDATE:` directives. Subject is auto-derived from key prefix using `ORG_MEMBER_NAMES` (e.g., `alex_training_level` → subject `alex`).

**Categories:** Free-form (no validation). Common categories include documents, training, travel, vehicle, health, finance, credentials, contact, bot, operations, organization, activities, general.

**Version chaining:** When a fact's value changes, the old fact is marked `expired=1` and `superseded_by` points to the new fact ID. Full audit trail preserved.

**Doc pointers:** Facts can link to files via `doc_pointer` (e.g., passport scan path). Shown as `[doc:path]` in search results.

**Fact-RAG linkage:** Each fact stores `source_msg_id` (message that created it) and `source_chunk_id` (nearest RAG chunk). RAG search results display linked facts inline.

### RAG Semantic Search (bot/storage/rag.py)

Chunk-based semantic search using Gemini embeddings:

- **Chunking:** Size-aware character-budget chunking (default 2000 chars per chunk, max 7 messages). Chunks grow until they hit the char budget or message cap. Long messages (>budget) auto-split into overlapping sub-chunks (200 char overlap). Consecutive chunks overlap by 2 messages for context continuity.
- **No truncation:** Every character of every message is embedded and searchable — no `[:N]` caps.
- **Embeddings:** `gemini-embedding-001` (3072-dim vectors), stored as float32 blobs
- **Search:** Cosine similarity, threshold 0.25, top-K results
- **Filtering:** Skips status/placeholder/system messages
- **Backfill:** Full rebuild via `rag_backfill` tool. Incremental updates on new messages
- **Results include:** chunk text, similarity score, msg ID range, linked facts, drill-down instructions

### Media Cache

Permanent media storage with AI descriptions and graceful degradation:

- **Retention:** 99 years (36135 days) — effectively permanent
- **Auto AI descriptions:** Every incoming photo is analyzed by Gemini Flash vision (`integrations/gemini.py:describe_media()`). Descriptions stored in `media_cache.description`, indexed via FTS5 UPDATE trigger on `media_cache_fts`. Config: `MEDIA_DESCRIPTION_ENABLED` (default true), `MEDIA_DESCRIPTION_BACKFILL_LIMIT` (default 20 per startup).
- **Startup backfill:** On restart, `_backfill_media_descriptions()` finds media with empty descriptions and generates them (crash recovery safety net).
- **TG re-fetch:** Stores `tg_file_id` for each Telegram media. If local file is missing, re-downloads from Telegram servers
- **Message linking:** Messages link to media via `media_cache_id` column
- **Status icons in search:** ✅ available, 🔄 re-fetched, ❌ unavailable
- **Degradation:** When media file is missing and can't be re-fetched, model receives a descriptive placeholder instead of the file

### Unified Search (memory_search tool)

One tool runs all four search backends in parallel:

1. **FTS5 keyword search** on messages (BM25 ranking)
2. **FTS5 search** on structured facts (key, value, category)
3. **Semantic RAG search** (meaning-based, cosine similarity)
4. **Media search** on `media_cache_fts` (filenames, AI descriptions, sender names)

Returns merged results in 4 sections: FACTS → EXACT MATCHES → SEMANTIC MATCHES → MEDIA MATCHES. Each RAG chunk includes linked facts inline and drill-down instructions. Media results include AI-generated descriptions, enabling search by image content (e.g. "contract scan", "receipt").

**Other search tools:**
- `search_facts` — browse/filter facts by category
- `search_media` — browse/filter media by type, search by content description
- `get_messages_by_range` — zoom into specific msg ID range or date range (returns full text + linked facts)
- `get_message_context` — expand around a single message

## Data Flow

### Message Processing
1. TG handler or Teams webhook receives message
2. Sender validated against unified chat registry + ADMIN_USERS; message deduped via SQLite unique index (source, chat_id, message_id)
3. Message stored in SQLite via `bot/storage/memory.py`
4. Media cached with permanent retention; TG file_id stored for re-fetch
5. `triage_and_enqueue()` enqueues to `PersistentQueue` (SQLite-backed, crash-safe, FIFO eviction)
7. PersistentQueue dispatches with concurrency control (MAX_PARALLEL_TASKS per chat, MAX_TOTAL_TASKS global)
8. `process_incoming()` builds system prompt, sets per-session reply-threading state (via `set_reply_threading()` → `_session_state[session_key]`), creates/resumes SDK client
9. SDK agent loop executes (tool calls, thinking, responses)
10. TG: streaming placeholder edits with 60s heartbeat. WA: per-task status updates with reply-threading (2.5min interval)
11. Fallback: if agent didn't call send tool, forward response text to chat
12. Post-processing: facts extraction (FACTS_UPDATE with version chaining), RAG indexing

### Scheduled Tasks
1. `scheduler_loop()` runs every 30 seconds
2. `get_due_tasks()` checks time, day, last_run
3. For each due task: creates a fresh SDK client, executes task prompt
4. Task marks itself as run via `mark_task_run()`

## Data Files

```
data/
  prompts/              — System prompt fragments (00-20, loaded in order)
  org_config.json       — Organization identity, members, goals (not in git)
  conversations.db      — SQLite: messages, message_facts, RAG chunks, media
  scheduled_tasks.json  — Scheduled task definitions
  tmp/                  — Temporary files (screenshots, downloads)
  media_cache/          — Cached media files
  google-workspace-creds/ — Google OAuth credentials
```

## Security Model

### Goals
1. **Container isolation** — bot in Docker cannot exploit or gain host-level access
2. **Least privilege** — all processes (container and host) run as non-root `botuser` (UID 10001)

### Container Hardening (Dockerfile + docker-compose.yml)

| Layer | Implementation |
|-------|---------------|
| **Non-root user** | `USER botuser` (UID 10001, GID 999/hostdocker) in Dockerfile — no root, no gosu, no setuid |
| **No Docker socket** | `/var/run/docker.sock` is NOT mounted — bot cannot control Docker |
| **Cap drop ALL** | `cap_drop: [ALL]` — no Linux capabilities |
| **No privilege escalation** | `security_opt: [no-new-privileges:true]` |
| **Read-only mounts** | Code, prompts, config, SSH keys mounted as `:ro` (google-creds writable for token refresh) |
| **Deploy via trigger files** | Bot writes JSON to `/deploy-triggers/` (bind-mounted `deploy/triggers/`) — host watcher reads directly |
| **No Docker CLI** | Docker, docker-compose not installed in container image |
| **Memory limit** | 8GB cap via `deploy.resources.limits.memory` |

### In-Container Defense (bot/hooks.py)

| Layer | Implementation |
|-------|---------------|
| **User allowlist** | Only registered chats + `ADMIN_USERS` can trigger inference — rejected at webhook level |
| **Admin-only tools** | Bash, Write, Edit, deploy_bot, deploy_status restricted to admin user IDs |
| **Bash blocklist** | Blocks: `docker`, `pip install`, `tee`/`dd` to sensitive paths, inline `subprocess`/`socket`/`child_process`, `deploy_trigger.json` |
| **Path blocklist** | Blocks access to: `.env`, `entrypoint.sh`, `docker-compose`, SSH keys, Dockerfile, `deploy_trigger.json` |
| **MTProto auth** | Pyrogram session-based auth (no webhook) — session file protected by file path blocklist |

### Host-Side Isolation (deploy watcher)

| Layer | Implementation |
|-------|---------------|
| **Dedicated host user** | `botuser` (UID 10001, `nologin` shell) — runs the watcher, nothing else |
| **Docker group only** | botuser in `docker` group — can manage containers but not sudo |
| **Root-owned watcher** | Script installed to `/opt/<project>-watcher/` owned by root — botuser can execute but not modify |
| **Systemd service** | Runs as `User=botuser`, `Group=docker` — auto-restart, logging via journald |
| **SSH isolation** | botuser has its own deploy key in `/home/botuser/.ssh/` — separate from admin user |
| **Repo access** | Repo group set to `docker` (shared group) — botuser can read for git pull, no access to admin's other files |
| **Token refresh cron** | Host cron (admin user) refreshes OAuth token every 2h via `docker exec` — prevents silent 401 expiry |

### Self-Upgrade Flow

```
Bot (container)                    Host (watcher)
─────────────                      ──────────────
1. Edit code in /host-repo/
2. git commit + push (deploy key)
3. Write trigger JSON to
   /deploy-triggers/deploy_trigger.json
                                   4. Poll detects trigger file
                                   5. git pull origin main
                                   6. docker compose build --no-cache
                                   7. docker compose up -d
                                   8. Health check (120s timeout)
                                   9. Auto-rollback if unhealthy
                                   10. Write result JSON
Bot reads result from
/deploy-triggers/deploy_result.json
```

### Volume Ownership

All Docker volumes use UID 10001 (botuser). For existing deployments migrating from root/UID 1000:
```bash
for vol in bot-data bot-logs claude-sessions media-cache; do
  docker run --rm -v "${COMPOSE_PROJECT_NAME}_${vol}:/data" alpine chown -R 10001:10001 /data
done
```

## Known Issues

- WA media: bridge stores at `/app/store/`, bot-core reads from `/app/wa-data/` — path translation required
- Host-repo bind mount assumes host docker group is GID 999 (standard on most distros). If different, adjust Dockerfile `groupadd -g` and rebuild
- Host repo needs `chmod -R g+w` for container self-upgrade writes (setup wizard handles this)
- Deploy key uses `.container` copy pattern: original at 600 (host SSH), copy at 640/docker group (container mount). SSH rejects group-readable keys, so both copies are needed
- WA images: reported as attached but files sometimes not found at mapped paths
- Docker file mount placeholder: if a file mount source doesn't exist at container creation, Docker creates a directory placeholder. `docker compose restart` keeps the broken mount — must use `down` + `up` to fix. Entrypoint detects this for SSH key and logs a warning
- Deploy triggers must use bind mount (`./deploy/triggers:/deploy-triggers`), not Docker volume — volume mountpoints are root-owned and unreadable by non-root watcher
- Staging `deploy_staging` trigger dir: in prod container, `/deploy-triggers` is the PROD watcher's bind mount. `_staging_trigger_dir()` uses `IS_STAGING` flag (not path existence) to route staging triggers to `/host-repo/deploy/staging-triggers` in prod

## Staging Environment

Isolated staging instance for testing before prod deploys. See `CLAUDE.md` for full setup guide.

### Host Path Mapping
```
PROD REPO:    /home/botuser/corporate-bot/        (owner: botuser:docker)
STAGING REPO: /home/botuser/corporate-bot-staging/      (owner: botuser)
```
- Prod watcher polls: `/home/botuser/corporate-bot/deploy/triggers/`
- Staging watcher polls: `/home/botuser/corporate-bot/deploy/staging-triggers/`
- Staging watcher rebuilds in: `/home/botuser/corporate-bot-staging/`

### Key Architecture
- **Separate clone**: `/home/botuser/corporate-bot-staging/` (independent from prod at `/home/botuser/corporate-bot/`)
- **Compose override**: `docker-compose.staging.yml` — isolated volumes, port 8100, no camofox
- **Compose project**: `corporate-bot-staging` (via `-p` flag — REQUIRED to avoid clobbering prod)
- **Env override**: `.env.staging` — disables TG/WA/Vapi/relay
- **OAuth sharing**: Prod's `claude-sessions` volume mounted read-only at `/prod-claude` (explicit external volume `prod-claude-sessions` with `name: corporate-bot_claude-sessions`); startup copies `.credentials.json`
- **DB snapshot**: `scripts/staging_snapshot_db.py` — WAL-safe backup with credential scrubbing (57+ sensitive facts)
- **Shared network**: `bot-shared` external Docker network for prod↔staging HTTP
- **Test endpoint**: `POST /test/inject` (staging only) for synthetic message injection
- **Deploy tools**: `deploy_staging` + `deploy_staging_status` (admin-only, no review/QA gates)
- **QA gate**: `staging_qa_run` validates scenarios against staging via HTTP before prod deploy
