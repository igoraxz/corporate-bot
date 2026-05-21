---
description: "QA testing methodology for staging validation: test generation patterns, staging-aware rules, validation strategies, and the staging QA pipeline integration."
---

# QA Testing — Staging Validation Methodology

Governs QA testing within the staging environment. Referenced by the coding-principles
pipeline (Stage 11) and the self-upgrade deploy checklist (Step 5).

## Staging QA Pipeline

The staging QA gate is a MANDATORY step before any prod deploy (except restart).

### Flow

1. `staging_qa_run(regression=true)` loads 8 scenarios from `tests/staging_scenarios.json`
2. Cleans stale QA sessions via `/test/reset-clients` (prefix: `qa_`)
3. Deploys to staging if not already at HEAD (via `_staging_deploy_lock`)
4. Executes ALL scenarios in PARALLEL — each with unique chat_id (`qa_<ts>_<i>`)
5. Validates: tool calls, response text, blocked tools
6. Kills per-scenario SDK sessions on timeout via `/test/reset-clients`
7. If all pass -> `qa_staging_passed` flag set -> `deploy_bot` unblocked
8. If staging down -> admin override (tool verifies unreachability)

Target: ~2-3 min total (parallel), ~$0.50-1.00 per run.

### Two Modes

**Regression mode** (preferred for routine deploys):
`staging_qa_run(regression=true)` — loads 8 scenarios from `tests/staging_scenarios.json`.
Per-scenario unique chat_ids, all executed in parallel. Cost: ~$0.50-1.00 per run.

**Dynamic mode** (for ad-hoc/change-specific tests):
`staging_qa_run(scenarios="[{...}]")` — pass a JSON array of scenarios. Useful for testing
specific new functionality beyond what the regression suite covers.

### Gate Chain

deploy_review_passed -> qa_staging_passed -> deploy_confirmed -> deploy_bot

All flags auto-reset on any Edit/Write/Bash to /host-repo/.

## Static Regression Scenarios (`tests/staging_scenarios.json`)

The file contains 8 scenarios covering:
- Core inference: basic arithmetic (7+5=12)
- Language matching: Russian in -> Russian out
- Security: non-admin bash blocked by hooks
- Lightweight commands: /claude-set-oauth pre-SDK handler
- PM tools: admin can call pm_list_projects (tool registration)
- PM gate: non-admin blocked from PM tools (access control)
- Admin bash: admin can run Bash end-to-end (tool execution pipeline)
- Sandbox PM: non-admin without allowed_tools blocked from PM

### Assertion Types

- `expect_tools` — all listed tools must appear in response
- `expect_no_tools` — none of listed tools may appear
- `expect_text_contains` + `match_any=false` (default) — ALL patterns must appear (case-insensitive)
- `expect_text_contains` + `match_any=true` — at least ONE pattern must match
- `expect_text_not_contains` — none of listed patterns may appear (case-insensitive)

## Adding New Scenarios

When adding functionality that should be regression-tested:
1. Add a scenario to `tests/staging_scenarios.json`
2. Keep `timeout` <= 60s per scenario (total suite should complete in <90s)
3. Use `expect_text_match_any=true` for language-sensitive responses
4. Test scenarios against staging manually first via `/test/inject`

## Test Generation Patterns (for dynamic mode)

### Pattern 1: Tool Invocation Test
- Send a message that should trigger the tool
- Set expect_tools to verify the tool was called

### Pattern 2: Behavior Test
- Send a message that exercises the new behavior
- Verify response exists (non-timeout)

### Pattern 3: Error Handling Test
- Send deliberately bad input
- Verify bot responds gracefully (no crash)

### Pattern 4: Access Control Test
- Test with is_admin: false -> verify admin tools NOT called
- Test with is_admin: true -> verify admin tools ARE called

### Pattern 5: Content Verification Test
- Send a question with known answer
- Use expect_text_contains to verify correctness


## Pitfalls — Learned the Hard Way (Apr 2026)

### Model Self-Censorship vs Hook Enforcement
The model reads `is_admin` from session metadata and the system prompt's "Bash is
admin-gated" rule. When `is_admin=False`, the model REFUSES to call admin-gated tools
(Bash, Write, Edit, PM tools) BEFORE hooks even fire. `custom_instructions` in harness
config cannot reliably override this — the system prompt takes priority.

**Impact on scenario design**: You CANNOT test "sandbox user calls Bash successfully
via allowed_tools whitelist" because the model won't attempt the call. Instead:
- Test the POSITIVE case (admin CAN call the tool) with `is_admin: true`
- Test the NEGATIVE case (non-admin is blocked) with `is_admin: false`
- The hook enforcement IS tested by the negative case (hooks block if model attempts)

### ADMIN_USERS Set Contamination
`config.ADMIN_USERS` is a module-level mutable set. Synthetic admin entries added by
`test_inject` with `is_admin: true` persist for the process lifetime. If a non-admin
scenario reuses the same `user_id` as a prior admin scenario, `is_admin_user()` returns
True from the stale entry.

**Fix in test_inject**: Admin scenarios default to real admin ID (already in ADMIN_USERS).
Non-admin scenarios default to `"staging-nonadmin"` (never in ADMIN_USERS). Registration
uses `is_admin_user(source, uid)` per-user check, not broad source-level guard.

### Cross-Scenario State Contamination
Each scenario MUST use its own unique `chat_id` (`qa_<ts>_<i>`). If scenarios share a
chat_id, the SDK session retains context/state from earlier scenarios:
- Harness overrides (data_scope=admin) persist
- Model context includes prior conversation (affects tool selection)
- Session state (is_admin flag in user_ctx) carries over

**Fix**: Per-scenario chat IDs + SDK session reset in `test_inject` cleanup block +
per-scenario kill via `/test/reset-clients`.

### user_id Default Was Always Admin
Original code: `user_id = body.get("user_id", next(iter(TG_ADMIN_CHAT_IDS), 0))`.
This gave every inject the admin's real ID (999999999), so `is_admin_user()` returned
True regardless of the `is_admin` flag. Non-admin scenarios silently ran as admin.

**Fix**: Admin-aware default: `is_admin=true` → first real admin ID, `is_admin=false` →
`"staging-nonadmin"`.

### PostToolUse Buffering for expect_tools
`expect_tools` assertions only work if tool calls are captured. Send tools (telegram_send_message
etc.) are buffered by their MCP handlers via `staging_buffer_response()`. But non-send tools
(Bash, pm_list_projects, etc.) were invisible to QA assertions.

**Fix**: `staging_buffer_tool_call()` in PostToolUse hook — buffers tool name + args
WITHOUT signaling task completion (inject keeps waiting for final send_message).

### Parallel Execution Requirements
Sequential QA execution caused timeout cascades — if scenario A timed out, scenario B
queued behind it on the same session, exhausting its own timeout while waiting. Fix:
parallel execution via `asyncio.gather` + per-scenario chat_ids (no session sharing) +
per-scenario timeout with kill-on-timeout.

## Validation Rules (applied by staging_qa_run tool)

- PASS: Response exists (status=ok), expected tools called, no blocked tools, text assertions pass
- FAIL: Timeout, wrong tools, blocked tool called, empty response, text assertion failure
- Overall: ANY failure -> gate NOT set, must fix and re-run
