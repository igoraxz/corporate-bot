---
description: "Bot self-upgrade rules: source code locations, architecture reference, deploy checklist, auto-rollback, and self-upgrade safety rules."
---

# Self-Upgrade (Admin Only)

You have SDK built-in tools to read, search, edit, and deploy your own source code. These are ADMIN-ONLY tools — only admin users can use them.

**⛔ HARD DEPENDENCY: coding-principles skill MUST be loaded before using this skill.**
This skill cannot be used standalone — it depends on coding-principles for the review
pipeline (security-reviewer, quality-reviewer agents), tier classification, and the
12-stage review pipeline. If coding-principles is not loaded, invoke it via the Skill tool NOW
before proceeding with any code changes. All Architect Principles and General Coding
Rules from coding-principles apply here in full.

**IMPORTANT**: Read `/host-repo/CLAUDE.md` FIRST before making any code changes. It contains the full architecture, coding rules, common pitfalls, and deployment procedures. Everything below is a summary.

### Available tools (SDK built-in):
- `Grep` — Search for patterns across source files. USE THIS FIRST to find what you need.
- `Glob` — List files by glob pattern (e.g. 'bot/*.py')
- `Read` — Read a source file (supports line offset/limit for specific sections)
- `Edit` — Edit a file via search-and-replace (exact text match required)
- `Write` — Create/overwrite a file
- `Bash` — Run shell commands (use for git, deploy, docker, etc.)

### Admin MCP tools (from `mcp_tools.py`):
- `deploy_bot(action, reason)` — trigger rebuild after commit+push
- `deploy_status()` — check last deploy, includes failed startup logs
- `switch_model(model)` — per-chat model override; session resets after current query
- `switch_effort(level)` — runtime effort level (low/medium/high/max)
- `extend_budget(delta_usd)` — raise the CURRENT session USD cap without losing
  context. Keyword-gated: the user MUST say the budget-extend phrase in their
  CURRENT message. NEVER auto-invoke; only invoke after explicit request.
  Caps: per-call MAX_EXTEND_DELTA_USD (100), total MAX_TOTAL_BUDGET_CAP (500).
  Disconnect deferred to end-of-query so the tool reply reaches the user;
  next query spawns fresh CLI with --resume UUID and new cap.
- `usage_report(action)` — cost summary / top queries / rate limits

### Source code locations:
- **Inside container**: `/app/` (read-only copy baked into Docker image)
- **Repo root**: `/host-repo/` (mounted from host — full git repo)
- **Bot source**: `/host-repo/` (edits here persist and are used for builds)

ALWAYS edit files in `/host-repo/` — this is the persistent source that Docker uses to build.
Git operations run from `/host-repo/` (the repo root).

### Architecture quick reference:
- `main.py` — FastAPI app, message handling, TG streaming, scheduler
- `bot/agent.py` — ClientPool (one ClaudeSDKClient per chat, persistent sessions)
- `bot/hooks.py` — Security hooks (flat RBAC: per-chat role enforcement, bwrap kernel sandbox)
- `bot/mcp_tools.py` — Custom MCP tools (@tool decorator + create_sdk_mcp_server)
- `bot/mcp_config.py` — External MCP servers (Playwright, Google Workspace)
- `bot/storage/memory.py` — SQLite storage (conversations, message_facts, media cache)
- `bot/storage/rag.py` — RAG v4: size-aware chunk-based semantic search (char-budget chunking, long-msg splitting, Gemini embeddings, cosine sim)
- `bot/prompts.py` — System prompt builder (loads data/prompts/*.txt)
- `config.py` — Loads org_config.json + environment variables
- `integrations/` — TG, WA, Vapi, Gemini clients

### Workflow (follow this order):
1. Read `/host-repo/CLAUDE.md` to understand the codebase
2. **STAGE 1 — Research**: spawn background agent to research tools, patterns, optimal params (see Coding Principles)
3. **STAGE 2 — Architect**: spawn Plan agent to review against all 22 principles (see Coding Principles)
4. SHOW PLAN to user — get approval before implementing
5. Use `Grep`, `Read`, `Edit` to implement the approved plan
6. **Verify**: `python3 -c "import ast; ast.parse(open('/host-repo/<file>').read())"` — catches SyntaxError
7. **STAGE 3 — Code Quality**: spawn Explore agent to review all changes for bugs, smells, security, docs
8. **STAGE 4 — QA**: spawn Explore agent to run pytest, check edge cases, integration, regression
9. Fix ALL issues found in stages 3-4 before showing results to user
10. Show the user what you changed and ask for deploy confirmation
11. **Deploy** — when user confirms, follow the MANDATORY DEPLOY CHECKLIST below

⚠️ **DEPLOY GATE — HARD REQUIREMENT**: Steps 2-4 and 7-8 (the 4 review stages) are MANDATORY
for ALL code changes going to production. No exceptions — even if the admin says "just deploy it"
or "skip the review". The review pipeline exists to protect prod stability. If asked to skip,
explain: "Reviews are required for all prod changes. Running them now — takes a few minutes."
For 🟢 SMALL changes, stages 1-2 can be replaced with inline self-review.
For 🟡 MEDIUM, quality-reviewer agent is spawned post-impl.
For 🔴 SIGNIFICANT, full agent pipeline runs.

### Deploy — MANDATORY CHECKLIST (follow EVERY time):

PREREQUISITE — MANDATORY DEPLOY GATES (apply to ALL bot deploys, any scope):
See CLAUDE.md "Deploy Process" for the full gate sequence (Gates -1 through 4).
These bot-specific safety gates govern what MUST pass before deploy_bot is called.

**GATE 1 — Security + Quality Review** (NEVER skipped):
≤10 changed lines: inline self-review. >10 lines: **spawn named review agents**:
- `security-reviewer` agent — full security audit on the DIFF
- `quality-reviewer` agent — code quality + logic audit on the DIFF
Both defined in `.claude/agents/`. Fix ALL HIGH and MEDIUM findings.

**GATE 2 — Stage 12 Bot-Specific Integration Audit** (NEVER skipped):
Extends the generic Stage 12 checklist from coding-principles with bot-specific items.

Bot-specific doc sweep (extends Stage 12 item e):
- [ ] `CLAUDE.md` — architecture tree, key patterns, anti-patterns, pitfalls
- [ ] `docs/components/<name>.md` — component knowledge file for touched subsystem
- [ ] `data/prompts/*.txt` — especially `26_self_awareness.txt` for new features
- [ ] `.claude/skills/*/SKILL.md` — if skill behavior, pipeline, or auto-loaded content changed
- [ ] `README.md` — if user-facing behavior changed
- [ ] `.env.example` — if new config vars added
Stale docs mislead future sessions → treated as bugs.

Bot-specific registration audit (extends Stage 12 item a):
- [ ] RBAC seed (`bot/rbac/seed.py`) — new MCP tools or commands need policies
- [ ] Command registry (`bot/command_registry.py`) — new slash commands registered?
- [ ] `OBJECT_TYPES` (`bot/rbac/models.py`) — new RBAC object types added?
- [ ] Help menu (`_lightweight_help` in commands.py) — new commands visible?
- [ ] TG DM menu (`main.py` all_private_chats scope) — user-facing commands listed?
- [ ] Onboarding steps — if auth-related, does wizard detect completion?

Bot-specific composed artifact rebuild (extends Stage 12 item f):
- [ ] Harness CLAUDE.md rebuild — if skills or modules changed, harnesses with
      `claude_md_modules` will get stale composed CLAUDE.md until restart/rebuild.
      Verify `ensure_harness_project_dir()` will pick up the changes.
- [ ] Module manifest (`data/module_manifest.json`) — source→module dependencies current?
- [ ] Skills awareness prompt (`data/prompts/19_skills_awareness.txt`) — new skill listed?

Bot-specific auto-escalation files (forced 🔴):
- `bot/rbac/` (any file), `bot/auth/`, `bot/hooks.py`, `bot/core/access.py`
- `bot/harness.py` (security fields), `bot/mcp/__init__.py` (tool registration)
- `scripts/sandbox-exec.sh`, `scripts/sandbox-proxy.py`, `seccomp-bwrap.json`
- `scripts/desktop-relay/guard.sh`
- New storage files, DB schema changes, persistent data format changes (JSON stores, registries)

**GATE 3 — Stale Code + Dead Code Detection** (NEVER skipped):
Search for dead handlers, unreachable paths, deprecated references, unused imports.

**GATE 4 — Fact Hygiene**: Expire stale facts. Update session summary. Mark completed TODOs.

**GATE 5 — OSS Readiness**: Scan DIFF for instance-specific data in source. Must live in config/prompts/.env/facts.

**GATE 6 — Backup Flow Support** (when adding new persistent data):
Verify new volumes/DBs/configs are in `scripts/backup.sh` AND `scripts/restore.sh`. Skip if no new persistent state.

⚠️ **SESSION CHECK**: Deploy MUST ONLY be triggered from the MAIN conversation session
where the admin explicitly approved. Spawned agents, background tasks, and parallel sessions
MUST NOT call deploy_bot — they must return to the main session for admin approval first.

⚠️ **STAGING CONFIRMATION — SOFT GATE (no hook, self-enforced)**:
Before deploying code to staging, ALWAYS present completed changes to the operator and wait
for explicit confirmation to deploy staging. The operator may want to review, modify, or
adjust changes before they hit staging. Flow:
1. Complete code changes + commit + push
2. Show the operator a summary of what changed
3. Wait for operator to say "deploy staging" / "test it" / "go ahead"
4. THEN deploy to staging via `deploy_staging`
5. Run regression suite
6. Get operator confirmation for prod deploy
Do NOT auto-deploy to staging without operator awareness — staging deploys cost inference
and the operator may have additional changes to batch before testing.

When the user sends `/deploy` (or `/deploy no-qa` or `/deploy force`), you MUST execute ALL of
the following steps IN ORDER. Do NOT skip any step. Do NOT jump straight
to deploy_bot — it will FAIL if you haven't committed and pushed first (pre-deploy gates block it).
Note: Only the `/deploy` command sets the deploy confirmation flag. Casual mentions of "deploy"
in conversation do NOT authorize deployment. `/deploy force` skips all hook gates.
`/deploy no-qa` skips the staging QA gate only.

**STEP 1 — Verify syntax** (all changed files):
```bash
python3 -c "import ast; ast.parse(open('/host-repo/<file>').read())"
```

**STEP 2 — Commit changes** (REQUIRED — deploy_bot checks for clean working tree):
```bash
cd /host-repo && git add <changed-files> && git commit -m "bot-self-upgrade: <description>

Ticket: PM-<id>
Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```
**PM linkage**: Include `Ticket: PM-<id>` trailer from Stage T0. The deploy_bot tool
auto-parses these trailers to link tickets to deployments in the PM system.

**STEP 3 — Push to remote** (REQUIRED — deploy_bot checks for unpushed commits):
```bash
cd /host-repo && git push origin main
```

**STEP 3.5 — Run pipeline orchestrator** (REQUIRED — hook-enforced gate):
```
pipeline_orchestrate(ticket_id=<active_ticket_id>)
```
Independent server-side verification. Checks TCD gates + runs AI code review.
Sets HMAC-signed approval token. Deploy is BLOCKED without this gate.
If FAIL: fix findings and re-run. Loop until PASS.

**STEP 4 — Run deploy_review** (REQUIRED — hook-enforced gate):
```
deploy_review()
```
Runs automated syntax + import checks on ALL .py files. Sets `deploy_review_passed` flag
in session state. deploy_bot is BLOCKED without it. The flag auto-resets whenever you
Edit/Write/Bash any file in `/host-repo/`, so you MUST re-run after any fix.

**STEP 5 — Run staging QA** (REQUIRED — hook-enforced gate):
```
staging_qa_run(regression=true)
```
Loads regression scenarios from `tests/staging_scenarios.json` and executes them against
staging via HTTP `/test/inject`. Each scenario gets its OWN unique chat_id (`qa_<ts>_<i>`)
for full session isolation — prevents admin state (data_scope=admin, harness overrides)
from leaking between scenarios. Sets `qa_staging_passed` flag. deploy_bot is BLOCKED without it.
Both flags reset together on any code edit. Assertions: `expect_tools` works for ALL tools
including Bash and PM tools (PostToolUse buffering via `staging_buffer_tool_call`),
`expect_no_tools`, `expect_text_contains`, `expect_text_not_contains`.
- For 🔴 SIGNIFICANT: `regression=true` (full suite) — covers core inference, language matching,
  security (admin bash blocked, prompt injection), tools, and help. Cost: ~$0.50-1.00.
- For 🟡 MEDIUM: `regression=true` (same suite — it is cheap enough to always run full).
- For 🟢 SMALL: call with override=true, reason="small tier" (skips testing).
- Admin override (override=true) only available when staging health check fails.
- Ad-hoc scenarios: pass `scenarios="[...]"` JSON for change-specific tests (optional, additive).

**STEP 6 — Trigger deploy**:
```
deploy_bot(action="rebuild", reason="<description of changes>")
```
Then check status with `deploy_status()`. Bot restarts automatically in ~30-120s.

⚠️ **WHY THIS ORDER MATTERS**: The `deploy_bot` tool has hard-blocking pre-deploy gates:
- Gate 0a: deploy_review — BLOCKS if deploy_review not run (Step 4 fixes this)
- Gate 0b: staging_qa_run — BLOCKS if staging QA not passed (Step 5 fixes this)
- Gate 2: Clean working tree — BLOCKS if uncommitted changes exist (Step 2 fixes this)
- Gate 3: Commits pushed — BLOCKS if unpushed commits exist (Step 3 fixes this)
If you skip Steps 2-5 and call deploy_bot directly, it will REJECT the deploy and nothing happens.
The host watcher does `git pull` before building — so unpushed code never reaches the build.

**FALLBACK — Manual deploy via Bash (only if watcher not available)**:
Same Steps 1-3 above, then:
```bash
cd /host-repo && docker compose build --no-cache bot-core
# WARNING: next command kills YOUR container — nothing after executes
docker rm -f corporate-bot 2>/dev/null || true
cd /host-repo && docker compose up -d bot-core
```
If the build fails, revert: `cd /host-repo && git reset --soft HEAD~1`

### Auto-rollback on failed deploy:
The host watcher automatically verifies health after every deploy (`/health` endpoint,
120s timeout). If the health check fails, it auto-reverts to the **last known healthy commit**
(stored in `deploy/last_healthy_commit`), rebuilds, and restarts. This means even if the bot
made multiple commits before triggering a deploy, rollback targets the last verified-healthy
state — not just the commit before the most recent pull.

The healthy commit file is initialized on first watcher startup and updated after every
successful deploy health check. The deploy result file (`deploy/deploy_result.json`) will
show status `rolled_back` with the failed and restored commit hashes.

Build failures also auto-revert to the last healthy commit so the repo stays on a working state.

Manual fix is only needed if both the deploy AND rollback fail (status `rollback_build_failed`):
```bash
docker logs corporate-bot --tail 20   # See the error
git log --oneline -5               # Find a working commit
git reset --hard <working-commit>
docker compose build --no-cache bot-core
docker rm -f corporate-bot; docker compose up -d bot-core
```

### Mac Daemon Sync (post-deploy for relay changes):

When daemon code changes (`scripts/desktop-relay/sdk_persistent_daemon.py`), after deploy:
1. `desktop_relay(action="install_daemon")` — stages updated daemon on Mac for approval
2. Operator runs `~/.relay/approve-install.sh` on Mac terminal to review diff + approve
3. `desktop_relay(action="install_status")` — checks approval status + auto-respawns daemons

Similarly for Mac skills: `desktop_relay(action="install_skills")` stages for approval.
Both require Mac-side `approve-install.sh` before going live (FB-40 security gate).

### Editing Skills and Prompts:

Skill files (`.claude/skills/**/SKILL.md`) and prompt fragments (`data/prompts/*.txt`)
are editable via `Edit` / `Write` / `Bash` tools — but Edit/Write require a harness-level
permission grant in `.claude/settings.json`:

```json
{"permissions": {"allow": [
  "Edit(/host-repo/.claude/skills/**)",
  "Write(/host-repo/.claude/skills/**)"
]}}
```

Without these entries, the Claude Code CLI refuses Edit/Write on skill paths as "sensitive files"
(even though `hooks.py` allows them). Bash (`cat >`, `cp`, `sed -i`) always works for admin
regardless of the allow-list — use Bash as a fallback if Edit/Write is blocked.

NOTE: settings.json changes only take effect after the bot container restarts (next deploy).
If the allow-list was just added/changed, Edit/Write will still be blocked in the current session
— use Bash until the next restart.

### Self-Upgrade Observability:

When ANY new feature, parameter, or metric is added:
1. Add appropriate logging (structured, with key=value pairs)
2. Update the daily_selfopt scheduled task prompt to monitor the new metric
3. If the feature has tunable parameters, document them in CLAUDE.md
4. Ensure the self-optimize task can detect degradation and recommend adjustments

This is NOT optional — observability is a first-class requirement for every change.

### Configuration Management — Prod vs Staging

The bot maintains TWO independent environments with SEPARATE configuration state.
Understanding which environment you're configuring is critical — mistakes leak across.

**ENVIRONMENT AWARENESS — MANDATORY**:
Before changing ANY configuration (harnesses, scheduled tasks, OAuth, model overrides),
determine from context whether the change targets PROD or STAGING:
- Self-upgrade / bot fixes / production features → PROD
- AB tests / pipeline experiments / QA scenarios → STAGING
- If ambiguous → ASK: "Should this go to prod or staging?"
NEVER assume — a harness created on prod when meant for staging wastes resources and
pollutes the production config. A staging config applied to prod can break live users.

**Config file locations** (isolated by Docker volumes):

| Config | Prod (bot-data volume) | Staging (staging-bot-data volume) |
|--------|----------------------|----------------------------------|
| Harnesses | /app/data/harnesses.json | same path, different volume |
| Chat harnesses | /app/data/chat_harnesses.json | same path, different volume |
| Model overrides | /app/data/model_overrides.json | same path, different volume |
| OAuth overrides | /app/data/oauth_overrides.json | same path, different volume |
| Scheduled tasks | /app/data/scheduled_tasks.json | same path, different volume |
| SQLite DB | /app/data/conversations.db | same path, different volume |

All paths are IDENTICAL inside the container — isolation comes from Docker named volumes.
Changes inside one container do NOT affect the other. To configure staging, you must
execute commands inside the staging container or use the staging API.

**Access restrictions — WHO manages WHAT**:

Bot can manage on its own (via MCP tools or direct file writes):
- Harness definitions and assignments (manage_harness tool)
- Model/effort/OAuth per-chat overrides (switch_model, switch_oauth)
- Scheduled tasks (manage_scheduled_task)
- Facts and knowledge graph (manage_fact, FACTS_UPDATE)
- Prompt files (data/prompts/*.txt)
- Skills (.claude/skills/*/SKILL.md)
- Claude OAuth profiles (/claude-auth slash command, manage_oauth tool)

Admin manages on host system (bot CANNOT modify):
- .env file — API keys, service credentials
- data/org_config.json — organization identity, member IDs
- data/google-workspace-creds/ — Google OAuth
- Docker Compose files — service definitions, volumes, networks
- Host watcher / systemd services — deploy infrastructure
- TG session files — Pyrogram MTProto auth keys
- MS365 auth — requires host-side scripts/m365_login.py
- Backup config (.backup-password)

Exceptions — bot-manageable auth:
- Claude OAuth (/claude-auth <label>) — bot runs claude setup-token in-container
- OAuth profile management (manage_oauth add/list/remove)
- These are the ONLY auth items the bot can create/rotate independently

**Configuring staging** (from prod container):
For config that lives in Docker volumes (harnesses, scheduled tasks, etc.), use the
staging HTTP API or inject a configuration script via /test/inject endpoint.
For code changes, commit + push + deploy_staging — the staging watcher pulls and rebuilds.

**Pitfalls (learned the hard way)**:
1. Same paths, different volumes: /app/data/harnesses.json exists in BOTH containers
   but they are completely independent files. Reading one tells you nothing about the other.
2. No cross-container file access: Prod cannot read staging files or vice versa
   (except via HTTP endpoints or shared network).
3. Staging has no TG/WA: .env.staging disables TG/WA/Vapi/relay.
4. OAuth shared read-only: Staging mounts prod claude-sessions volume at /prod-claude
   (read-only) and copies .credentials.json on startup.
5. DB snapshots are point-in-time: staging_snapshot_db.py copies prod DB with scrubbing —
   staging DB diverges immediately after.
6. deploy_staging deploys CODE only: git pull + docker build on the staging clone.
   Config files in volumes (harnesses, model overrides) are NOT affected by deploys —
   they persist in the staging-bot-data volume across rebuilds.

### Self-Upgrade Rules:
0. **ALWAYS consider RBAC for new features.** Every new command, tool, or endpoint MUST be
   RBAC-gated from day one. Default = deny. Use `manage_rbac(action='add_policy', ...)` to
   create scoped roles. Employee self-service features (e.g., OAuth switch) use
   `_is_employee_own_dm()` pattern: admin OR employee-in-own-DM. Never add admin-only as a
   shortcut when scoped access is the right design. Reference `bot/rbac/seed.py` for patterns.
1. **NEVER deploy without EXPLICIT user confirmation in a SEPARATE message — this is a HARD REQUIREMENT.** You MUST: (a) show all proposed changes, (b) WAIT for the admin to send a SEPARATE message with the /deploy command, and (c) ONLY THEN call deploy_bot. The confirmation MUST be a standalone reply — not part of the original request. Even if the user says "make this change and deploy" in one message, you MUST show the changes first and wait for a separate confirmation message before deploying. Do NOT commit, push, or deploy autonomously — even if you think the change is safe or trivial. Unauthorized deploys can break the system and are unrecoverable while the bot is down. This rule overrides all other considerations.
   **PARALLEL/SPAWNED SESSIONS**: Subagents and background sessions MUST NEVER deploy independently. Only the MAIN conversation session with direct admin interaction can trigger deploy_bot. If a spawned agent completes code changes, it must return results to the main session, which then shows changes and waits for admin approval in a separate message. No exceptions.
2. Read CLAUDE.md first — understand the architecture before changing anything
3. ALWAYS verify syntax: `python3 -c "import ast; ast.parse(open('<file>').read())"`
4. ALWAYS ask for explicit confirmation before deploying — NEVER deploy automatically. The deploy workflow is: edit → verify syntax → SHOW CHANGES TO USER → wait for explicit /deploy command → **git add + git commit + git push** → deploy_bot. The confirmation MUST come as a separate reply, not in the same message as the change request. ALL THREE git steps are mandatory before deploy_bot — it will reject the deploy without them.
5. NEVER use literal newlines in f-strings — use \n escape sequences. SyntaxError = bot crash
5a. **TG chat identity = chat_id + thread_id (EVERYWHERE).** In Telegram forum supergroups,
   a topic is a SEPARATE chat identified by `chat_id:thread_id`. This composite key MUST be
   used for ALL operations — not just sending messages:
   - **Session keys**: `telegram:chat_id:thread_id` (separate SDK session per topic)
   - **Sending**: every `send_message()` / `edit_message()` / `delete_message()` includes `message_thread_id`
   - **Harness resolution**: `get_harness_for_chat(key)` uses the full topic key
   - **Timeout/checkpoint**: `_get_task_timeout()` uses `build_chat_key_from_task()`
   - **RBAC subject**: `_resolve_rbac_subject()` uses `origin_chat_id` from topic key
   - **Message storage**: `store_message()` includes `message_thread_id` column
   - **Chat registry**: `is_authorized()` checks topic key, NOT parent group
   - Builder: `build_chat_key(source, chat_id, thread_id)` from `bot/chat_key.py`
   - `int(chat_id)` must use try/except, not `isdigit()`
6. NEVER edit .env files or credential files (blocked by security layer)
7. WA media paths: bridge uses `/app/store/`, bot-core uses `/app/wa-data/` — always translate
8. After deploy, warn user about ~15s downtime. Prefer deploy_bot tool over manual docker commands
9. If you add new volumes/dirs, add `chown botuser:botuser` in entrypoint.sh
10. ALWAYS commit and push to remote after each change — never accumulate uncommitted changes across multiple updates. Workflow: edit → verify syntax → commit → push → deploy.
11. Each update must be a separate commit with a descriptive message. Do NOT batch unrelated changes into one commit.
12. **ALWAYS use standard git commands** (`git add`, `git commit`, `git push`). NEVER work around git internals — no custom `GIT_OBJECT_DIRECTORY`, no manual copying/moving of `.git/objects/`, no `git update-ref`, no index manipulation. If a normal `git commit` fails (permissions, corruption, etc.), report the error to the admin and stop. Do NOT attempt creative workarounds — they make things worse.
