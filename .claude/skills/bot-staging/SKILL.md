---
description: "Staging environment: architecture, test_inject endpoint, QA runner, OAuth sharing, pitfalls, host path mapping."
---

## Staging Environment

Isolated staging instance for testing without affecting production TG/Teams sessions.

### Architecture
- **Config**: `BOT_ENV` env var (`production`|`staging`), `IS_STAGING` bool in `config.py`
- **Parametrized naming**: All instance-specific values derived from `COMPOSE_PROJECT_NAME` env var. See `docs/ORCHESTRATION.md` for full parametrization reference.
- **Separate clone**: Staging uses its own git clone at `~/${COMPOSE_PROJECT_NAME}-staging/` (fully independent from prod). `.env` symlinked from prod (shared secrets), `.env.staging` tracked in git (staging overrides).
- **Compose override**: `docker-compose.staging.yml` — separate volumes, parametrized container name (`${COMPOSE_PROJECT_NAME}-staging`), no camofox
- **Env override**: `.env.staging` — disables TG/Vapi/relay, cheaper model defaults
- **OAuth sharing**: Prod's `claude-sessions` and `bot-data` volumes mounted read-only. On startup, staging copies `.credentials.json` + OAuth profiles from prod. **Runtime management**: `staging_command` MCP tool proxies commands (switch_oauth, manage_oauth) to staging via `/test/inject`.
- **DB snapshot**: `staging_snapshot_db.py` creates WAL-safe backup with credential scrubbing (57+ sensitive facts removed). Copy into staging volume for realistic test data.
- **Lifespan guards**: TG client, restart notifications skipped when `IS_STAGING`
- **Deploy watcher**: `deploy/staging-watcher.sh` — host-side script in `/opt/${COMPOSE_PROJECT_NAME}-staging-watcher/` (root-owned). Polls trigger dir in prod repo. Systemd service template: `deploy/staging-watcher.service` (CHANGE_ME pattern).
- **MCP tools**: `deploy_staging` (trigger rebuild/restart + repo sync + commit verification), `deploy_staging_status` (check result + failed logs), `staging_command` (run any command on staging at runtime). Admin-only. No deploy_review/qa gates — staging IS the QA target
- **Staging cross-session coordination** (`bot/mcp/deploy.py`):
  - `_staging_deploy_lock` (`asyncio.Lock`): held exclusively by `deploy_staging()` for the full rebuild cycle (trigger write → watcher result, max 5 min). Prevents two sessions racing to rebuild staging simultaneously. On success, sets `_staging_last_deployed_commit = HEAD`; the next `deploy_staging` caller skips a rebuild if staging is already at HEAD.
  - `staging_qa_run()` does NOT hold the lock. It peeks: if `_staging_deploy_lock.locked()`, it waits up to 3 min for the deploy to finish, then immediately releases (peek pattern). Multiple parallel QA runs are allowed — isolated via unique per-run chat ID prefixes (`qa_<ts>_<i>`).
  - **Parallel scenario execution**: `staging_qa_run` runs all scenarios concurrently via `asyncio.gather`. Total QA time ≈ max(individual scenario time) instead of sum. Each scenario fires independently, kills its own session on timeout/error via `/test/reset-clients?prefix=qa_<ts>_<i>`, and reports results collected after all coroutines complete.
- **Failure handling**: On build or health check failure, captures container logs to `staging-triggers/staging_last_failed_startup.log`. No auto-rollback — bot investigates failures and pushes fixes

### Key Components
- **`/test/inject` POST endpoint** (staging only, 404 in production): inject synthetic messages into the processing pipeline. Body: `{"text": "...", "chat_id": "...", "timeout": 60, "harness_label": "...", "harness_config": {...}}`. Timeout capped at 3600s (PM in-session execution with review agents may take 20-40 min). Admin injects get zombie detection disabled (query_timeout_s=0) so long-running MCP tool calls (pm_run_sprint) aren't killed. `harness_config` dict auto-creates the harness on staging if it doesn't exist (harnesses.json is per-volume); `project_dir` auto-set for non-admin data_scope. Returns `{"status": "ok", "responses": [...]}` with buffered tool calls.
- **`/test/responses/{chat_id}` GET/DELETE**: poll/clear buffered staging responses
- **`/test/reset-clients` POST endpoint** (staging only): disconnect SDK clients matching a prefix. Body: `{"prefix": "qa_"}`. Used by QA runner to clean stale sessions before/after runs.
- **`staging_buffer_response()`**: buffers send_message tool calls in `_staging_responses` dict AND signals task completion so `/test/inject` returns
- **`staging_buffer_tool_call()`**: buffers non-send tool calls (Bash, pm_list_projects, etc.) WITHOUT signaling completion — called from PostToolUse hook on staging; enables `expect_tools: ["Bash"]` assertions while keeping inject waiting for the final send_message
- **`scripts/staging_snapshot_db.py`**: WAL-safe prod DB backup with credential scrubbing
- **`scripts/run_staging_tests.sh`**: orchestrates unit + live staging tests
- **`tests/test_staging.py`**: unit tests (config, buffer, snapshot) + live tests (endpoints, isolation)

### Usage
```bash
# All examples use $P = COMPOSE_PROJECT_NAME from .env (e.g. corporate-bot)
# See docs/ORCHESTRATION.md for full reference.

# Setup staging clone (one-time):
sudo -u botuser git clone git@github-private:user/repo.git /home/botuser/$P-staging
sudo -u botuser ln -s /home/botuser/$P/.env /home/botuser/$P-staging/.env
sudo -u botuser ln -s /home/botuser/$P/data/google-workspace-creds /home/botuser/$P-staging/data/google-workspace-creds

# Start staging (from staging clone, MUST use -p, BOT_PORT as shell env):
cd /home/botuser/$P-staging
sudo -u botuser BOT_PORT=<staging_port> docker compose \
  -p $P-staging \
  -f docker-compose.yml -f docker-compose.staging.yml \
  up -d --no-deps bot-core

# Stop staging:
cd /home/botuser/$P-staging
sudo -u botuser docker compose -p $P-staging down

# Snapshot prod DB for staging:
docker compose exec $P python3 /host-repo/scripts/staging_snapshot_db.py \
  --src /app/data/conversations.db --dst /app/data/staging-snapshot.db

# Copy snapshot into staging volume:
docker cp $P:/app/data/staging-snapshot.db /tmp/staging-snapshot.db
docker cp /tmp/staging-snapshot.db $P-staging:/app/data/conversations.db
sudo -u botuser docker compose -p $P-staging restart bot-core

# Test inject:
curl -X POST http://localhost:8100/test/inject -H 'Content-Type: application/json' -d '{"text": "hello"}'

# Unit tests only (no server needed):
python -m pytest tests/test_staging.py -v -k "not Live"

# Start staging + run all tests:
./scripts/run_staging_tests.sh --live
```

### Pitfalls (learned the hard way)
- **`-p` flag is REQUIRED**: Without it, staging and prod share the same Compose project — starting one RECREATES the other's container
- **`BOT_PORT` must be shell env**: Compose variable substitution reads shell env + `.env` file, NOT the service `env_file` directive. `.env.staging` cannot override the port
- **Run as botuser**: Repo is owned by botuser (UID 10001). Running as the host user causes permission errors on `.env.staging` and other files
- **Scripts path**: Inside the container, scripts are at `/host-repo/scripts/`, NOT `/app/scripts/`
- **Alpine no-op for camofox**: Can't use `profiles` to disable it (makes service "undefined", breaks `depends_on` validation). Alpine stub keeps it "defined" but harmless
- **`depends_on: {}`** does NOT clear inherited values in Compose v2 — it merges (appends nothing)
- **Staging trigger dir**: `_staging_trigger_dir()` must use `IS_STAGING` flag, NOT path existence — `/deploy-triggers` exists in prod too (it's the prod watcher's bind mount). Using `is_dir()` causes `deploy_staging` to accidentally rebuild prod
- **Stale SDK sessions + cross-scenario state contamination**: `staging-claude-sessions` volume persists across container rebuilds. Two isolation levels required: (1) **Cross-run**: use a fresh timestamped prefix per run (`qa_<ts>_`), clean stale sessions via `/test/reset-clients` with prefix `qa_` before each run. (2) **Cross-scenario**: each scenario MUST use its own unique chat_id (`qa_<ts>_<i>`) so admin state set by scenario A (e.g. `data_scope=admin` override, harness assignment) does NOT leak into non-admin scenario B running on the same session. Example failure: `pm_tools_exist` (admin) sets `data_scope=admin` override → `pm_admin_gate` (non-admin, same session) sees admin scope → PM tools NOT blocked → test fails. Fix implemented in QA runner: per-scenario chat IDs + run-prefix cleanup in step 4.
- **QA runner runs in prod**: `staging_qa_run` MCP tool executes in the PROD container — staging only hosts the `/test/inject` endpoint. Changes to the runner code require a PROD deploy to take effect (chicken-and-egg: use `force=true` when the runner itself is broken)
- **PM agents on staging — /host-repo is read-only**: PM pipeline agents (Engineer) get `cwd=/app/data/harness-projects/pm-agents/` which IS writable, but task descriptions referencing `/host-repo` paths will fail writes. For AB testing on staging: use relative paths in task descriptions (the Engineer's cwd is writable) or avoid hardcoding `/host-repo`. The pipeline code itself does NOT reference /host-repo — it comes from the task description flowing through TCD.
- **PM pipeline timeout**: PM v5 in-session execution (plan + implement + review agents) can take 15-30 min per ticket. With fix loops (max 3 iterations), worst case ~60 min. The inject timeout cap (3600s) and zombie detector (disabled via query_timeout_s=0 for admin injects) must both accommodate this. Without these overrides, the task is killed partway through.
- **Staging volume conflict — `external` + `driver` merge crash**: If a volume is declared `external: true` in `docker-compose.yml` AND `driver: local` in `docker-compose.staging.yml`, Compose v2 merges them into `{external:true, driver:local}` which is invalid → instant `build_failed` (sub-second, no real build happens). Symptom: staging watcher logs show "❌ Staging build FAILED" instantly. Fix: use different Compose-level keys for prod vs staging declarations of the same physical Docker volume — only the `name:` field needs to match the actual volume name.
- **OAuth default without a profile file**: The default profile label (from `oauth_default_label.json` or `OAUTH_DEFAULT_LABEL` env var) is just a label — it does NOT switch to a different account unless a matching profile file exists at `OAUTH_PROFILES_DIR/<label>.json`. Without a profile file, the bot falls back to `~/.claude/.credentials.json` (auto-refresh). To use a specific account as default: ensure the profile JSON exists, then call `manage_oauth(action="set-default", label="...")` or set `OAUTH_DEFAULT_LABEL` in `.env`. See `get_default_oauth_label()` / `get_oauth_token()` in `config_overrides.py`.
- **Lightweight commands bypass SDK → staging QA can't capture responses**: Pre-SDK handlers (e.g. `_lightweight_switch_oauth`) call `_send_to_platform_simple()` which sends directly to Telegram/Teams without going through the SDK tool mechanism. The staging `/test/inject` framework only captures `staging_buffer_response()` calls made from SDK tool handlers. Fix: in `_send_to_platform_simple`, check `IS_STAGING` and call `staging_buffer_response()` before the real network send — then return early so the response IS captured by the QA framework.
- **PM scenario timeout cascade in regression suite**: If scenario A (e.g. `pm_tools_exist`) times out from the QA runner's perspective, the SDK query keeps running. Scenario B (e.g. `pm_admin_gate`) is enqueued on the SAME `chat_id` and must wait behind A's still-running query — exhausting B's own timeout while queued. Fix: (a) generous per-scenario timeouts (300s each) in `tests/staging_scenarios.json`; (b) per-scenario unique chat_ids so scenarios never share the same session queue.
- **Staging watcher build errors → check systemd journal, not watcher log**: `deploy/staging-triggers/staging_watcher.log` only captures the watcher's startup banner and trigger detection lines. Docker build output (including Go compilation errors, Docker layer failures, config validation errors) goes to the systemd journal. To see real build errors: `journalctl -u staging-watcher -n 200 --no-pager` on the host. `deploy_staging_status` shows the OLD container's runtime logs (not build errors).
- **Harness definitions are per-volume, not git-tracked**: `harnesses.json` lives in the `bot-data` Docker volume. Staging has its own `staging-bot-data` volume with separate harness definitions. Harnesses created on prod are NOT available on staging. Fix: `test_inject` accepts `harness_config` dict to auto-create harnesses on the staging instance when they don't exist. QA scenarios can include `harness_label` + `harness_config` to be self-contained. Without this, staging injects silently fall back to the `default` harness (which denies Bash/Write/Edit via settings.json).

- **Model self-censorship blocks non-admin tool testing**: When session metadata has `is_admin=False`, the model reads "Bash is admin-gated" from the system prompt and REFUSES to call Bash/Write/Edit/PM tools before hooks fire. `custom_instructions` in harness cannot override this. Impact: cannot test "non-admin sandbox user calls Bash via allowed_tools whitelist" — model won't attempt the call. Fix: test positive cases (admin CAN call tool) with `is_admin: true`, test negative cases (non-admin blocked) with `is_admin: false`.
- **ADMIN_USERS set contamination across injects**: `config.ADMIN_USERS` is a module-level mutable set. Synthetic admin entries from `test_inject(is_admin=true)` persist for the process lifetime. If user_id defaults to a real admin ID (e.g. 999999999 from `TG_ADMIN_CHAT_IDS`), ALL subsequent injects — even with `is_admin=false` — get admin access because `is_admin_user()` returns True from the stale entry. Fix: admin-aware user_id default (`staging-nonadmin` for non-admin) + per-user `is_admin_user()` check before registration.
- **SDK session reset required between scenarios**: Without `client_pool.reset_session()` in the `test_inject` cleanup block, the SDK subprocess retains prior conversation context, harness overrides, and user_ctx state. A non-admin scenario running after an admin scenario inherits admin context. Fix: reset SDK session + clear harness overrides in the post-inject cleanup block.
### Host Path Mapping — CRITICAL REFERENCE

```
PROD REPO:    /home/botuser/corporate-bot/        (owner: botuser:docker)
STAGING REPO: /home/botuser/corporate-bot-staging/      (owner: botuser)
```

| Component | Prod | Staging |
|-----------|------|---------|
| Compose files | `docker-compose.yml` | `docker-compose.yml` + `docker-compose.staging.yml` |
| Compose project | `corporate-bot` | `corporate-bot-staging` (via `-p`) |
| Container name | `corporate-bot` | `corporate-bot-staging` |
| Port | 8000 | 8100 |
| `/host-repo` mount | prod repo | staging repo (read-only) |
| `.env` | prod repo's `.env` | symlink → prod `.env` |
| `.env.staging` | n/a | tracked in git (overrides) |
| Deploy trigger dir | `deploy/triggers/` | n/a (staging is not self-deploying) |

**Deploy trigger flow:**
1. Bot (prod container) calls `deploy_staging` → writes to `/host-repo/deploy/staging-triggers/` = `/home/botuser/corporate-bot/deploy/staging-triggers/`
2. Staging watcher (systemd: `staging-watcher.service`) polls `/home/botuser/corporate-bot/deploy/staging-triggers/`
3. Watcher runs: `cd /home/botuser/corporate-bot-staging && git pull && docker compose -p corporate-bot-staging -f docker-compose.yml -f docker-compose.staging.yml up -d --build bot-core`
4. Watcher writes result JSON back to same trigger dir

**Prod deploy trigger flow:**
1. Bot calls `deploy_bot` → writes to `/host-repo/deploy/triggers/` = `/home/botuser/corporate-bot/deploy/triggers/`
2. Prod watcher (systemd: `host-watcher.service`) polls `/home/botuser/corporate-bot/deploy/triggers/`
3. Watcher runs: `cd /home/botuser/corporate-bot && git pull && docker compose build && docker compose up -d`

**NEVER confuse `~` paths** — `~` resolves to `/home/botuser` for the host user and `/home/botuser` for botuser. Always use absolute paths in docs and commands.

### Repo Isolation
Staging uses a separate git clone at `/home/botuser/corporate-bot-staging/`. The prod repo at `/home/botuser/corporate-bot/` is never touched by staging operations. Both repos track the same remote (`origin`). Shared config: `.env` symlinked from prod, `.env.staging` tracked in git, `data/google-workspace-creds` symlinked from prod.

### Volume Isolation
Staging uses isolated named volumes (`staging-bot-data`, `staging-bot-logs`, `staging-claude-sessions`, `staging-media-cache`) that never touch production data. Additionally, prod's `claude-sessions` volume is mounted read-only at `/prod-claude` for OAuth credential sharing (lifespan copies `.credentials.json` on startup). Staging clone is mounted read-only as `/host-repo`. Config files (prompts, org_config) shared read-only from staging clone.

### Network
Both prod and staging join the `corporate-bot-shared` external Docker network (`docker network create corporate-bot-shared`). This lets prod reach staging at `http://corporate-bot-staging:8000` for QA testing via `/test/inject`. Network is defined in `docker-compose.yml` (prod) and `docker-compose.staging.yml` (staging). Default `STAGING_URL` in `config.py` points to this address.
