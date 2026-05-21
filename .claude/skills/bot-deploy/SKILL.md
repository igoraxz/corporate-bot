---
description: "Deploy process: gates, host watcher, staging, rollback, failed deploy diagnostics, sensitive-file approval."
---

## Deploy Process

**CRITICAL: NEVER deploy without explicit `/deploy` command from the admin.** Show all changes, then wait for the admin to send `/deploy` (or `/deploy no-qa` or `/deploy force`). Casual mentions of "deploy" in conversation do NOT authorize deployment.

### Via deploy_bot tool (self-upgrade) — MANDATORY STEPS IN ORDER:
1. Edit code in `/host-repo/`
2. Verify syntax:
   - Python: `python3 -c "import ast; ast.parse(open('/host-repo/<file>').read())"`
3. **Run coding agent review gates** — REQUIRED for ALL self-upgrades (from self-upgrade + coding-principles skills):
   - **Stage 8 — security review**: spawn `security-reviewer` agent on the diff
   - **Stage 9 — quality review**: spawn `quality-reviewer` agent on the diff
   - Fix any CRITICAL/HIGH findings before continuing. Medium issues: document, fix or accept.
   - Additional checks: docs freshness, stale code detection, fact hygiene, OSS readiness — see coding-principles SKILL.md for full checklist
4. **Show changes to user and wait for explicit `/deploy` command**
5. **`git add <files> && git commit`** — REQUIRED (deploy_bot gate blocks on dirty working tree)
6. **`git push origin main`** — REQUIRED (deploy_bot gate blocks on unpushed commits)
7. **`deploy_review`** — REQUIRED hook-enforced gate. Runs syntax + import checks, sets `deploy_review_passed` flag. deploy_bot is BLOCKED without it. Flag auto-resets on any Edit/Write/Bash to `/host-repo/`.
8. **`staging_qa_run`** — REQUIRED hook-enforced gate. Runs QA test scenarios against staging via HTTP. Sets `qa_staging_passed` flag. deploy_bot is BLOCKED without it. Flag auto-resets on any Edit/Write/Bash to `/host-repo/`. Admin override available only when staging is unreachable (tool verifies).
9. `deploy_bot(action="rebuild", reason="...")` — triggers host watcher
10. Host watcher: git pull, docker build, restart, health check
11. On failure: captures container logs → auto-rollback to last healthy commit
⚠️ Steps 5-8 are NOT optional. deploy_bot will REJECT the deploy if you skip them.
⚠️ Step 3 (coding agent reviews) is NOT optional for code changes — they catch security issues and logic bugs that syntax checks miss.

### Pre-deploy gates (automated):

**Gate -1 — /deploy command (hook-enforced, in hooks.py PreToolUse):**
`deploy_bot` is BLOCKED unless the user's current message contains the `/deploy` command.
Casual mentions of "deploy" in conversation do NOT count — only the explicit `/deploy` prefix.
Variants: `/deploy` (all gates), `/deploy no-qa` (skip QA gate), `/deploy force` (skip ALL hook gates).
The `/deploy` command sets `deploy_confirmed_flag` in session state; `/deploy force` also sets
`deploy_force_flag` which bypasses gates 0, 0a, and 0b below. `/deploy no-qa` sets `deploy_noqa_flag`
which bypasses gate 0b only. Detection via `check_deploy_approval()` regex on the user's message text
(reply-quote blocks stripped to prevent bot echo self-authorization).

**Gate 0 — active sessions guard (hook-enforced, in hooks.py PreToolUse):**
`deploy_bot` is PAUSED if other active SDK tasks are running (non-idle). Lists active sessions
and asks admin to confirm interruption. Sets `deploy_active_sessions_acked` flag on first block —
second call passes through. Applies to all actions (rebuild/restart both kill sessions).
Prod only — staging deploys skip this check. Bypassed by `/deploy force`.

**Gate 0a — deploy_review (hook-enforced, in hooks.py PreToolUse):**
`deploy_bot` is BLOCKED unless `deploy_review` tool was called first in the same session.
`deploy_review runs 6 gates: syntax + imports + smoke tests + docs staleness + pyflakes + staging boot verification and sets `deploy_review_passed` flag in session state.
The flag auto-resets whenever Edit/Write/Bash modifies files in `/host-repo/`, forcing re-review.
'restart' action bypasses this gate.

**Gate 0b — staging_qa_run (hook-enforced, in hooks.py PreToolUse):**
`deploy_bot` is BLOCKED unless `staging_qa_run` tool was called first in the same session.
`staging_qa_run` executes QA test scenarios against the staging environment via HTTP (`/test/inject`),
validates responses (health canary + smoke canary + per-scenario checks), and sets `qa_staging_passed` flag.
Two modes: `regression=true` loads static scenarios from `tests/staging_scenarios.json` (zero LLM generation
cost, ~$0.50-1.00 per run), or `scenarios` JSON for dynamic/ad-hoc tests. Regression mode is preferred for
routine deploys. Assertions: `expect_tools`, `expect_no_tools`, `expect_text_contains`,
`expect_text_not_contains`, `expect_text_match_any`. Both flags (`deploy_review_passed`,
`qa_staging_passed`) auto-reset together on any code edit.
Admin override only when staging health check fails (tool verifies unreachability). 'restart' bypasses.
Bypassed by `/deploy no-qa` or `/deploy force`.

**Gates 1-4 (automated, in deploy_bot tool):**
The `deploy_bot` tool runs these checks automatically before writing the trigger file (for rebuild/rebuild-all actions):
1. **Syntax check** — parses ALL .py files in /host-repo with `ast.parse()`. Hard block on errors.
2. **Clean working tree** — `git status --porcelain` must be empty. Hard block if uncommitted changes.
3. **Commits pushed** — `git rev-list --count HEAD --not --remotes` must be 0. Hard block if unpushed.
4. **Integration tests** — runs pytest on tests/test_integration.py. Warning only (non-blocking).

'restart' action skips all gates (no code changes involved).

### Failed deploy diagnostics:
When a deploy fails health check, the host watcher:
1. **Captures container logs** (last 300 lines + container state) to `deploy/last_failed_startup.log`
2. Auto-rolls back to last healthy commit
3. `deploy_status` tool automatically includes the failed logs in its response
4. The bot can self-investigate failures without SSH access — just call `deploy_status`

Log file: `deploy/last_failed_startup.log` (accessible in bot-core at `/host-repo/deploy/last_failed_startup.log`)

### Manual:
```bash
git pull origin main
docker compose build --no-cache bot-core
docker compose up -d bot-core
```

## Testing Changes

Before deploying, verify:
1. No Python syntax errors: `python3 -c "import ast; ast.parse(open('<file>').read())"`
2. Import check: `python3 -c "from bot.<module> import <thing>"`
3. Health endpoint: `curl http://localhost:8000/health`
