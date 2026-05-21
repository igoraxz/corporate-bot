---
description: "Coding quality principles, 22-point architect checklist, 16 anti-patterns (AP-1 through AP-16), review pipeline with stages T0, SM, 0-12 with 3-tier enforcement (SMALL/MEDIUM/SIGNIFICANT), and plan format for ALL programming tasks."
---

# Coding Principles — Apply to ALL Programming Tasks

Governs ALL coding work (self-upgrade, new software, scripts, tools, debugging, external projects).
Code quality and design correctness matter more than speed. The review pipeline is NOT optional —
if asked to skip, reply: "Reviews are enforced by the pipeline. Running them now."

**FILE LOCATION — MANDATORY FOR BOT SELF-UPGRADE:**
ALL code edits for bot self-upgrade MUST be made in `/host-repo/` (the git repo mount), NEVER in
`/app/` (the running container). `/app/` is a Docker image — changes there are lost on rebuild
and must be manually copied to `/host-repo/` before committing. This is a waste of time.
- Investigation/reading: `/app/` is fine (it's the running code)
- Editing/writing code: ALWAYS `/host-repo/`
- If you catch yourself editing `/app/bot/...` instead of `/host-repo/bot/...` — STOP and switch.

---

## Change Tiers — Determines Which Stages Run

Classify the change BEFORE starting. When in doubt, tier UP.

- **🟢 SMALL** (≤3 files, single module, clear requirements — bug fix, config, helper, prompt edit): T0 → SM → 2 (inline) → 5 → 6 → 9 (inline) + 12 (inline) → 10 → deploy.
- **🟡 MEDIUM** (2-5 files, cross-module within same subsystem, moderate complexity): T0 → SM → 2 (inline) → 5 → 6 → 9 (agent) + 12 (inline) → 10 → deploy.
- **🔴 SIGNIFICANT** (≥5 files OR cross-subsystem OR security/auth-sensitive): T0 → SM → ALL stages 0-12, no skips. Stage 0 auto-triggered for new features.

**Classification decision tree:**
```
Security / auth / OAuth / credentials / tokens / concurrency / shared state?
  → 🔴 SIGNIFICANT (always, regardless of size)

≥5 files OR spans 3+ directories/subsystems?
  → 🔴 SIGNIFICANT

2-5 files OR cross-module within same subsystem?
  → 🟡 MEDIUM

Otherwise (≤3 files, single module, clear fix)?
  → 🟢 SMALL
```

**Forced 🔴 regardless of size** — any change touching: `hooks.py`, `entrypoint.sh`, `Dockerfile`,
`docker-compose`, `.env`, credentials, OAuth, auth/permission logic, deploy scripts, MCP tool security,
shared state / concurrency / module-level globals, access control policy definitions, credential
storage, token management, sandbox configurations, **new storage files / storage schema changes /
persistent data format changes** (DB schemas, JSON config stores, registry files, project metadata).

**Keyword auto-🔴** — if the task description or operator request contains ANY of these keywords,
classify as 🔴 SIGNIFICANT before further analysis: OAuth, RBAC, credential, token, auth,
encryption, session, permission, deploy key, access control.

**Auto-escalation rule**: If you discover mid-implementation that files in the forced-🔴 list are
touched but the tier was classified lower, STOP. Reclassify to 🔴 and inform the operator.
Do not continue with incomplete review coverage on security-sensitive changes.

---

## Review Pipeline — Stages T0 through 12

```
TASK → classify tier
  → [T0. PM Ticket]                             ← ALL TIERS (MANDATORY)
  → [SM. Subject Matter Research]               ← ALL TIERS (MANDATORY)
  → [0. Product Research]                       ← 🔴 new features only (optional)
  → [1. Deep Research]                          ← 🔴 only
  → [2. Architect Plan]                         ← 🔴 full / 🟡 🟢 inline
  → ┌ [3. Security Review (pre)]  ┐  PARALLEL   ← 🔴 only
    └ [4. Quality Review (pre)]   ┘
  → [5. USER APPROVAL]                          ← ALL TIERS (hard gate)
  → [6. Implementation]                         ← ALL TIERS
  → ┌ [7. Architect Review (post)]    ┐
    │ [8. Security Review (post)]     │
    │ [9. Quality Review (post)]      │ PARALLEL ← 🔴 all / 🟡 9+12 / 🟢 9+12 inline
    └ [12. Integration Audit (post)]  ┘
  → [10. Fix Issues]                            ← ALL TIERS
  → [11. Final Re-review]                       ← 🔴 only
  → [ORC. Pipeline Orchestrator]                ← ALL TIERS (MANDATORY)
  → PRESENT FOR DEPLOY
```

### Stage T0 — PM Ticket (🔴 🟡 🟢, ALL TIERS, MANDATORY when PM available)

**Before ANY work begins**, create a PM ticket to track this coding task.
Every coding task — even trivial config changes — gets a ticket for full history.

**ENVIRONMENT DETECTION — check once at task start:**
- **Server/bot sessions** (PM tools available: `pm_auto_ticket`, `pm_move_ticket`, etc.):
  PM is MANDATORY. Create tickets, link commits, track sprints. No exceptions.
- **Desktop/standalone sessions** (no PM tools — e.g., Mac Claude Desktop, local CLI):
  PM is unavailable. Skip T0 gracefully — proceed directly to Stage SM.
  The quality pipeline (SM through Stage 11) still applies in full.

**How to detect**: Check if `pm_auto_ticket` is in your available tools. If yes → PM mode.
If no (tool not found) → standalone mode. Do NOT ask the user — detect automatically.

**PM mode (server):**
1. Call `pm_auto_ticket(title="<concise task title>", tier="<small|medium|significant>",
   operator_request="<original user message>")`
2. Store the returned ticket ID in session state (it will be referenced in commits)
3. When committing, include git trailer: `Ticket: PM-<id>`
4. After completion, move ticket to done: `pm_move_ticket(ticket_id=<id>, to_status="done")`

**Standalone mode (desktop):**
1. Skip T0 entirely — no ticket creation
2. Commit messages use descriptive text only (no `Ticket:` trailer)
3. All other stages (SM, architect, security/quality review) still apply

**Why this matters**: PM tickets create a persistent audit trail linking operator requests
to code changes, commits, and deployments. Without a ticket, work is invisible to retros.

**For bug/subtask linking**: pass `parent_ticket_id` to link child tickets to the parent task.

**Multi-project PM**: Each project uses its own PM workspace. For external/standalone
projects, call `pm_create_project(name="<project>")` to create a dedicated project.
Never mix bot self-upgrade tickets with external project tickets.

### Sprint Assignment Rules (by tier)

Tier determines not just review depth but also sprint placement:

**🟢 SMALL** → rolling sprint (always).
`pm_auto_ticket(title=..., tier="small", ...)` — default behavior.

**🟡 MEDIUM** → rolling sprint initially.
`pm_auto_ticket(title=..., tier="medium", ...)` — starts in rolling sprint.
**Escalation trigger**: if PO requests 2+ follow-up tickets (feedback during/after impl or post-deploy):
1. Create individual sprint: `pm_create_sprint(project_id=..., name="<task-name>")`
2. Transition to running: `pm_transition_sprint` (approved → running)
3. Create follow-up tickets in the NEW sprint with `parent_ticket_id` linking to original
4. Original ticket stays in rolling sprint (completed there)

**🔴 SIGNIFICANT** → individual sprint (always).
1. `pm_auto_ticket(title=..., tier="significant", ...)` — gets project + ticket in rolling sprint
2. Immediately create individual sprint: `pm_create_sprint(project_id=..., name="<task-name>", goal="<operator request>")`
3. Transition to running: `pm_transition_sprint` (approved → running)
4. Create the main implementation ticket(s) in the individual sprint
5. ALL follow-up tickets (PO feedback, bugs, subtasks) go into the SAME individual sprint
6. Complete sprint when all work including follow-ups is done


### Follow-Up Ticket Rules

Follow-ups arise from PO feedback during implementation, after implementation, or after deploy.

For ALL follow-ups:
- ALWAYS set `parent_ticket_id` to the original ticket
- ALWAYS include `operator_request` with the PO's feedback text
- Tier the follow-up independently (a significant task may have small follow-ups)

Escalation decision:
- Original SMALL + 0-1 follow-ups → stay in rolling sprint
- Original SMALL + 2+ follow-ups → escalate: create individual sprint for follow-ups
- Original MEDIUM + 0-1 follow-ups → stay in rolling sprint
- Original MEDIUM + 2+ follow-ups → escalate: create individual sprint
- Original SIGNIFICANT → already in individual sprint (follow-ups join it)

### Gate Tracking (ALL TIERS, MANDATORY when PM available)

After completing EACH pipeline stage, record it on the PM ticket's TCD via `pm_update_ticket`.

**CRITICAL — TCD dict format:** The key MUST be `"pipeline_gates"` (NOT `"stages"`, NOT `"gates"`).
The orchestrator reads `tcd["pipeline_gates"]` — any other key name causes all gates to appear MISSING.

**Dict format for `pm_update_ticket(ticket_id=N, tcd=json_string)`:**
```json
{
  "pipeline_gates": {
    "T0": {"status": "passed", "notes": "ticket created"},
    "SM": {"status": "passed", "notes": "codebase explored"},
    "stage_2": {"status": "passed", "notes": "plan drafted"},
    "stage_5": {"status": "passed", "notes": "operator approved"},
    "stage_6": {"status": "passed", "notes": "implemented"},
    "stage_9": {"status": "passed", "notes": "quality review", "agent_id": "abc123"}
  }
}
```

Each gate entry requires: `"status"` (passed/failed/skipped/invalidated).
Optional: `"notes"`, `"agent_id"` (required for review stages 3,4,7,8,9,11), `"commit"`.

The TCD's `pipeline_gates` dict tracks every gate. This ensures NO stage is silently
skipped — even in long multi-task sessions where context drifts.

**Required gates by tier:**
- 🟢 SMALL: T0, SM, stage_2, stage_5, stage_6, stage_9, stage_12, stage_10, orchestrator
- 🟡 MEDIUM: T0, SM, stage_2, stage_5, stage_6, stage_9, stage_12, stage_10, orchestrator
- 🔴 SIGNIFICANT: ALL stages (T0, SM, stage_0 through stage_12) + orchestrator

**Pre-deploy validation:** Before deploying, call `check_gates_for_deploy(tcd)` —
returns (bool, missing_gates). Deploy is blocked if required gates are missing.

**After pivots/redesigns:** Call `invalidate_post_impl_gates(tcd)` to mark stages 7-11
as invalidated. Pre-implementation gates (T0, SM, 0-5) carry forward if the problem
didn't change. Re-run invalidated gates before deploying.

**Gate summary:** `format_gate_summary(tcd)` returns a compact status string for
ticket descriptions — shows ✅/❌/⏭️/🔄 per gate with commit refs.


### Model Confusion Discovery Rule

When you encounter a situation where you (the model) were confused by ambiguous documentation,
wrong key names, missing examples, or unclear instructions that caused a bug or wasted time:
1. Fix the immediate issue
2. **Suggest to the operator** creating a ticket to fix the root cause in the awareness
   modules, skills, or system prompt that caused the confusion
3. This prevents the same confusion from recurring in future sessions

Example: "I used `stages` instead of `pipeline_gates` because the docs showed a function
call but not the raw dict format. Suggest creating a ticket to add explicit dict examples
to coding-principles skill."

### Stage SM — Subject Matter Research (🔴 🟡 🟢, ALL TIERS, MANDATORY)

**Before any planning or implementation**, understand the system you are working in.
Implementing on wrong assumptions wastes time twice — once building the wrong thing, once
redoing it. This stage is the root-cause prevention for that vicious loop.

**For bot self-upgrade tasks:**
- Read `CLAUDE.md` — architecture, key patterns, anti-patterns, pitfalls
- Read the actual source of every module you will touch (Grep/Read, not from memory)
- Read relevant docs: `docs/components/<subsystem>.md` (component knowledge files), `data/prompts/`, `.claude/skills/`, `docs/ARCHITECTURE.md`
- Study the AP list (AP-1 through AP-16) — has this problem been solved before?
- **Confirm all ambiguities with the user before designing** — never assume; ask

**For any coding task:**
- Read existing code in all modules you will touch (Grep/Read/Glob before writing)
- Understand current architecture and how the change fits in
- Match existing patterns before inventing new abstractions
- Search web/docs if the domain involves unfamiliar libraries or APIs
- Stay focused — only research what is directly relevant

**T0 VERIFICATION**: Before proceeding, confirm T0 was completed. If PM is available
(tools detected) and no ticket was created, STOP and create the ticket NOW.

**Gate**: Do NOT proceed to Stage 0/2/6 until you have solid, verified domain understanding.
Unexpected complexity or contradictions with prior assumptions → STOP and confirm with user.

### Stage 0 — Product Research (🔴 new features only, optional)

Spawn `product-researcher` for NEW USER-FACING FEATURES (external users, market alternatives, business
implications). Skip for bug fixes, refactors, internal tooling, config/prompt edits. Can also run standalone
when the user explicitly asks for market/competitive analysis.

The agent picks from its 8-section framework (Market Landscape, Target Persona/JTBD, Feature Scoping,
PMF, Economics, GTM, Growth/Retention, User Stories). Output feeds Stage 1 + Stage 2.

### Stage 1 — Deep Research (🔴)

Spawn `code-researcher`. Three questions:
- **Library/tool selection**: Does an existing OSS solution fit? Evaluate maturity, maintenance, security.
  Prefer tuning an existing tool over building custom — if building custom, document WHY (security,
  integration, fit, supply chain). Compare top 2-3 candidates.
- **Parameters/thresholds**: What do production systems use? Find benchmarks, not guesses. Document
  "X because Y benchmark shows Z". No benchmark → define how to measure post-deploy.
- **Patterns/architecture**: Industry-standard approaches? How do systems at scale handle this? Trade-offs?

Include sources/links in the plan output.

### Stage 2 — Architect Plan

- **🔴**: Spawn `architect` agent — designs against all 22 principles, identifies risks/missed edges/simpler
  alternatives, checks research completeness, returns critique. Address each critique point before finalizing.
- **🟡**: Inline — answer the 5 self-review questions + present brief plan (PLAN FORMAT below). No subagent.

### Stage 3 — Security Review, Pre (🔴, parallel with 4)

Spawn `security-reviewer` on the PLAN. Checks: OWASP Top 10 mapping; data flow boundaries; credential
handling (storage, logs, commits); new attack surface (endpoints, tools, integrations); supply chain
(CVE, maintenance, licence); privilege escalation; container security (Dockerfile/compose/entrypoint/volumes).
Output: findings with severity (HIGH/MEDIUM/LOW).

### Stage 4 — Quality Review, Pre (🔴, parallel with 3)

Spawn `quality-reviewer` on the PLAN. Checks: all 22 principles; complexity (simplest? decomposable?);
test strategy (coverage, edges); documentation impact; rollback strategy (<60s). Output: findings with severity.

### Stage 5 — User Approval (ALL TIERS, HARD GATE)

Present: research summary (🔴), structured plan (PLAN FORMAT), security+quality findings (🔴), mitigations
for HIGH/MEDIUM. Wait for explicit "approved" / "go ahead" / "yes". Modifications → back to Stage 2.
Cancel / no → stop.

### Stage 6 — Implementation (ALL TIERS)

Implement exactly what was approved.
**T0 checkpoint**: Before writing the first file, confirm the PM ticket exists.
Every commit carries `Ticket: PM-<id>`. If T0 was skipped, create now.
Flag deviations: "This differs from plan because…". Never silently
change scope. Verify syntax after each file change.

### Stage 7 — Architect Review, Post (🔴, parallel with 8, 9)

Spawn `architect` to compare implementation vs approved plan: code matches plan point-by-point? Undocumented
deviations / scope creep? File changes match? Blast radius minimized? Reversible <60s? Output: deviation report.

### Stage 8 — Security Review, Post (🔴 ONLY — parallel with 7, 9)

Spawn `security-reviewer` on the DIFF: hardcoded secrets (high-entropy regex, key patterns); unsafe
eval/exec/subprocess(shell=True); path traversal; missing input validation on new params; file writes bypassing
path blocklist; bash commands vulnerable to injection; logs leaking sensitive data; TLS-less network calls.
Output: vulnerabilities with severity.

### Stage 9 — Quality Review, Post

**Per tier:**
- **🔴**: Spawn `quality-reviewer` agent on the DIFF (parallel with 7, 8)
- **🟡**: Spawn `quality-reviewer` agent on the DIFF (sole post-review agent)
- **🟢**: Inline self-review using the 5 self-review questions (NO agent)

Spawn `quality-reviewer` on the DIFF:
- Bugs: logic errors, off-by-one, races, null/None.
- Style: naming, organization, Python 3.12 typing (`dict`/`list`/`X | None`, not `Dict`/`List`/`Optional`).
- DRY: duplication from elsewhere.
- Dead code: unreachable branches, unused imports, commented-out blocks.
- Error handling: all new paths have handlers, errors surface to chat.
- Performance: O(n²), unbounded lists, missing pagination, unnecessary I/O.
- Logging: structured, timing on slow ops.
- Tests: pytest run if present, coverage of new paths.
- Config: no hardcoded values that should be externalized.
- **Docs smell check**: Are docs referencing changed code signatures/APIs still accurate?
  Flag obvious stale references. (Exhaustive doc audit is Stage 12's responsibility —
  Stage 9 catches glaring omissions during quality review.)

### Stage 10 — Fix Issues (ALL TIERS)

**HIGH/MEDIUM** must fix before deploy. **LOW** → show user, fix only if <2min each.
**FALSE POSITIVE** → document reasoning, skip.

### Stage 11 — Final Re-review (🔴 only)

Spawn `quality-reviewer` to verify: HIGH/MEDIUM fixes correct and complete, no new issues introduced,
nothing skipped, tests still pass. FAIL → back to Stage 10. PASS → present for deploy approval.

### Stage 12 — Integration Audit, Post (🔴 🟡 🟢)

Runs parallel with 7/8/9 (post-implementation). Verifies the change is fully integrated —
code that works but is not registered, documented, or discoverable is incomplete.

**Per tier:**
- **🔴**: ALL items mandatory. Document each answer.
- **🟡**: Items a, b, c, e mandatory. Others if applicable.
- **🟢**: Item e only (stale ref check). Others if applicable.

**Checklist (generic — project-specific extensions in deploy skill):**

a. **Registration audit**: Are new capabilities registered in the project's access control
   system? New tools/commands/endpoints need explicit permission grants. Default = deny.
   FAIL: new capability without access control registration = HIGH.

b. **Discoverability**: Can users find the new feature? Check help menus, command lists,
   documentation index. FAIL: new command/tool invisible to users = MEDIUM.

c. **Config registration**: New config vars / env vars documented with defaults and rationale?
   Staging overrides needed? FAIL: undocumented config var = MEDIUM.

d. **Stale data cleanup**: Expire stored facts, cached state, or persisted config that
   references removed/changed behavior.

e. **Doc & symbol sweep** (ALL TIERS): Grep for removed/renamed symbols across all doc files
   (`*.md`, `*.txt`, test fixtures). Every doc referencing changed code must be updated in
   the same commit. FAIL: stale reference to removed symbol = MEDIUM.

f. **Composed artifact rebuild**: If the change modifies skills, modules, or templates that
   are composed into downstream artifacts (e.g., per-project config files, harness CLAUDE.md),
   verify those artifacts will be rebuilt on deploy/restart.

**Project-specific extensions**: Each project's deploy skill (e.g., `self-upgrade`) adds
project-specific items to this checklist (e.g., RBAC seed policies, module manifest,
harness CLAUDE.md rebuild). The generic checklist above applies to ALL projects.



### Stage ORC — Pipeline Orchestrator (ALL TIERS, MANDATORY)

**After all review stages complete**, call `pipeline_orchestrate(ticket_id)`.
This is an independent server-side verification agent that:

1. **Mechanically verifies** all required TCD gates are present and passed
2. **Checks agent evidence** (agent_ids for 🔴 review stages)
3. **Runs independent AI code review** via separate Anthropic API call
4. **Sets HMAC-signed approval token** if everything passes

The orchestrator runs INSIDE the MCP tool handler — the main session cannot
interfere with the verification. The approval token is cryptographically signed
and verified at deploy time against the current commit hash.

**Flow:**
- All stages complete → call `pipeline_orchestrate(ticket_id)`
- If FAIL: fix the findings → re-call → loop until PASS
- If PASS: orchestrator gate set → can proceed to deploy

**Gate**: `orchestrator` is REQUIRED in TCD for ALL tiers. Deploy is blocked
without it. The HMAC token also resets on any code edit (same as deploy_review).

---

**Self-Review (🟡 inline or sanity check):**
1. Why not simpler? (Simplicity)
2. What becomes obsolete? Which docs need updating? (No Dead Code / No Stale Docs)
3. What happens on failure/restart? (Completeness + Graceful Degradation)
4. Does this introduce new attack surface? (Security)
5. What exact steps verify this works? (Testing)

---

## Architect Principles — 22-Point Checklist

Run mentally before presenting ANY plan. Each has a challenge question.

**A. DESIGN**

1. **SIMPLICITY FIRST** — Simplest solution that works wins. Flat > nested, concrete > abstract, explicit > implicit. → *"Why not simpler? Could we use fewer moving parts?"*
2. **YAGNI** — Solve the current problem only, not speculative futures. → *"Needed right now, or speculation?"*
3. **SEPARATION OF CONCERNS** — One responsibility per module. Prompts in prompt files, config in config, logic in code. **Instance-specific data (names, IDs, schedules, preferences) MUST live in config/prompts/.env, NEVER in source** — source must be generic enough to merge upstream without leaking private data. → *"If one part changes, will it drag unrelated parts with it?"*
4. **LEAST ASTONISHMENT** — Behaviour matches the name. No hidden side effects, surprising defaults, or magic. → *"Obvious to a reader in 3 months?"*
5. **MINIMAL COUPLING (Law of Demeter)** — Talk only to direct deps. No `a.b.c.d` chains. Pass data explicitly. → *"How many levels deep does this reach?"*

**B. CODE QUALITY**

6. **NO REDUNDANCY (DRY)** — Every piece of logic/config exists once. Two doing the same job → one must go. → *"Does this duplicate something existing?"*
7. **NO DEAD CODE / NO STALE DOCS** — Same commit that makes something obsolete must remove it. **Documentation is code** — every code change updates all affected `README.md`, `CLAUDE.md`, `.env`, comments, prompts. Stale docs actively mislead. → *"What becomes stale? Which docs need updating?"*
8. **SINGLE SOURCE OF TRUTH** — Every value has one canonical location; others reference. → *"Defined in exactly one place? Who owns it?"*
9. **COMPLETENESS** — Specify behaviour, edges, errors, restarts now. No "TODO: handle later". → *"Error? Empty input? Restart? Timeout?"*

**C. RESILIENCE & OPERATIONS**

10. **GRACEFUL DEGRADATION** — External failures → reduced functionality, not crash. Every external call has a failure path. → *"What if this dep dies?"*
11. **BLAST RADIUS MINIMIZATION** — Each change touches the smallest possible area. Small commits, isolated modules, clear boundaries. → *"If this breaks, what else fails?"*
12. **ROLLBACK-FIRST DESIGN** — Every deploy reversible in <60s (`git revert` + rebuild). Avoid data migrations/schema changes without backups. → *"Can we roll back in under a minute?"*

**D. OBSERVABILITY**

13. **OBSERVABILITY BY DEFAULT** — Logging + metrics from day one, not an afterthought. Log key decisions, state changes, timings. Structured (key=value). → *"Can we diagnose from logs alone?"*
14. **SELF-OPTIMIZATION AWARENESS** — Every tunable is externalized, measurable, and logged. New params must be discoverable/adjustable without code changes. → *"Can we measure this? How would someone tune it?"*
15. **CONFIGURATION AS CODE** — All tunables in config/env/facts with documented range, default, rationale. → *"Defaults documented with rationale?"*

**E. SECURITY & DEPLOYMENT**

16. **SECURITY BY DEFAULT** — New endpoints require auth. New data flows protect sensitive data. New deps require explicit approval (supply chain). Credentials: never logged, never in chat, never in git. All external input is untrusted. → *"New attack surface? Data exposure risk?"*
17. **DATA ACCESS ISOLATION** — Every data store/API/resource has explicit rules: WHO (role), WHAT (scope), HOW (enforcement). Default deny. Auditable (defined in config, not scattered). Centralized gate (no duplicated checks). Violations logged with full context. Credentials get extra isolation. Third-party gets minimal approved access. → *"Who can access? Where enforced? Default-deny? Auditable?"*
18. **DEPLOYMENT DISCIPLINE** — Separate commit per change with descriptive message. Verify (syntax/lint/type). **Commit + push BEFORE deploy** — deploy tools reject uncommitted/unpushed code. Sequence is ALWAYS: syntax → git add → commit → push → deploy_bot. Prefer automated deploy over manual. → *"Commit atomic, verified, pushed? Ran ALL steps in order?"*

**F. PROCESS**

19. **FEASIBILITY CHECK** — Verify the plan actually works in the target env: available APIs/endpoints, runtime setup, permissions, network, libraries. → *"Can this actually run in the target env?"*
20. **TESTING STRATEGY & QA** — Concrete verification steps (not "we'll check"). Positive + negative tests. Integration testing MANDATORY: unit/functional, integration with ALL touching systems, regression check. Any test fails → fix before deploy. → *"Exact verification steps? Integration points? ALL tests passed?"*
21. **PLAN ADHERENCE** — Implementation matches plan point-by-point. Deviations flagged: "This differs from plan because…". Never silently change scope. → *"Matches exactly? Deviations flagged?"*
22. **GOAL-DRIVEN EXECUTION** — Decompose into sub-goals with explicit success criteria. Execute → verify (syntax, test, log, expected output) → proceed. Verification fails → fix before moving forward. → *"Success criteria per step? Verifying before proceeding?"*

---

## Plan Format — Answers Pre-Embedded

When showing a development plan:

```
PLAN: [name]
GOAL: [one sentence]
TIER: [🟢 SMALL / 🟡 MEDIUM / 🔴 SIGNIFICANT] — [justification]
RESEARCH SUMMARY: [key findings + sources — 🔴 only]
CHANGES: [files + what changes]
SIMPLICITY CHECK: [why simplest adequate]
WHAT BECOMES OBSOLETE: [dead code/configs to remove]
OBSERVABILITY: [logging/metrics added]
DATA ACCESS: [who accesses what, where enforced, default-deny check]
TESTING & QA: [unit, integration, regression]
SECURITY: [pre-review findings + mitigations — 🔴 only]
QUALITY: [pre-review findings + mitigations — 🔴 only]
RISKS: [what could go wrong + rollback plan]
```

Eliminates back-and-forth.

---

## Pipeline Execution Discipline

**AFTER STAGE 5 (USER APPROVAL) — NO STOPPING UNTIL DEPLOY-READY:**

Once the user approves a plan, you are committed to executing ALL remaining stages
for the classified tier. Do NOT:
- Ask "should I continue?" or "want me to keep going?"
- Suggest "deploy what we have and do the rest later"
- Offer to "do X as a follow-up ticket" for work that belongs in THIS pipeline
- Stop after implementation without running reviews
- Stop after reviews without fixing findings
- Stop after fixes without running re-review (🔴 only)

The ONLY legitimate stops after approval:
1. A HIGH review finding that requires plan redesign → back to Stage 2
2. Budget/turn limit approaching → send partial results + "continue" mechanism
3. User explicitly says "stop" or "pause"

**TIER CLASSIFICATION IS A HARD GATE — NEVER DOWNGRADE SECURITY:**

If ANY file in the change touches auth, credentials, RBAC, OAuth, tokens, hooks,
or security enforcement: the tier is 🔴 SIGNIFICANT. Period. This is not a
suggestion — it is an absolute rule. "Only 2 files" or "simple change" does NOT
override the forced-🔴 list. Misclassification leads to deploying unreviewed
security code.

🔴 SIGNIFICANT means: ALL stages 0-12, including pre-reviews (3/4) and
final re-review (11). Skipping any stage is a pipeline violation.

---

## General Coding Rules

**The Four Cardinal Rules (Think, Simplify, Surgical, Goal-Driven) are in the system
prompt at `data/prompts/21_coding_behavior.txt` — always active, survive compaction.
These rules below are coding-specific operational rules that complement them.
Overlap is intentional — system prompt rules set behavioral defaults;
checklist items provide verification gates. Do NOT remove either layer.**

1. ALWAYS grep/search before reading — understand what exists first.
2. ALWAYS use offset/limit for large files (>200 lines).
3. ALWAYS show the user what you plan to change before editing.
4. Make SURGICAL changes — don't refactor surrounding code, don't "improve" adjacent code.
5. NEVER update dependency versions without explicit approval (supply chain; all deps pinned).
6. Python 3.12 typing: `dict`, `list`, `str | None` — NOT `Dict`, `List`, `Optional`.
7. `/host-repo` is for **bot self-upgrade ONLY**. All other coding tasks (PM pipeline, AB tests,
   standalone projects) MUST write to their own workspace (cwd or harness project dir).
   Read `/host-repo` for reference patterns — never write to it unless the task is upgrading
   the bot itself. On staging, `/host-repo` is read-only and writes will fail.
8. **External projects get the same quality standards**. These principles apply equally to
   bot self-upgrade, external repos, standalone scripts, and employee workstation projects.
   The quality pipeline (SM, architect, review) is universal — only PM tracking is env-dependent.

---

## Learned Anti-Patterns — Real Failures, Rules Earned

Each rule comes from a production issue that had to be fixed. Violating any is a SERIOUS failure.

### ⛔ AP-1: VERIFY ASSUMPTIONS BEFORE PROPOSING SOLUTIONS
Solutions that depend on runtime behaviour (propagation, inheritance, ordering) must be verified
BEFORE proposing — read the source, trace the call stack, `grep` for callers. "It should work" ≠
"I verified in source at line X that it works". (ContextVars incident: proposed for parallel isolation,
but SDK hooks inherit context from `connect()`, not `query()` — approach was wrong and had to be redesigned.)
→ *"Have I verified this mechanism, or am I assuming?"*

### ⛔ AP-2: REUSE EXISTING CONFIG — NEVER INVENT NEW PARAMETERS
Before adding any new config/env var/constant, `grep` for existing values with the same meaning.
Derive from what exists. New config is a last resort. (Invented `TG_ADMIN_CHAT_ID` when `ADMIN_USERS`
already held the same data.)
→ *"Does an existing config value already contain this?"*

### ⛔ AP-3: SIMPLEST SOLUTION THAT WORKS — NOT THE CLEVEREST
Before building anything, ask: "Can I solve this with a simple function using existing data?" A filter
function beats a classification pipeline every time. Build infrastructure only when the problem genuinely
requires it. (Built a message classification system with DB columns and tagging when a 20-line filter
using existing data would do.)
→ *"Am I building infrastructure, or solving the problem?"*

### ⛔ AP-4: USE EXISTING DATA FIELDS — DON'T REBUILD WHAT EXISTS
Before designing, inventory what's already in the system (DB schemas, existing fields, stored metadata).
The answer is often already there. (Built noise filtering by analysing message content when a `msg_type`
field already distinguished user from bot messages.)
→ *"What data do I already have that solves this? Checked the DB schema?"*

### ⛔ AP-5: PICK THE RIGHT LAYER — CODE vs MODEL vs HYBRID
- **Code**: deterministic ops, data lookup, validation, fast classification with clear rules.
- **Model**: nuanced judgment, tone, creative output, complex intent.
- **Hybrid**: code for deterministic part (detection/extraction), feed into model for judgment.
Often beats either alone. (Language detection: pure code failed at nuance, pure model drifted across
compaction — hybrid langdetect + model-in-prompt was robust.)
→ *"Which layer handles this best? Could hybrid be more robust?"*

### ⛔ AP-6: DIAGNOSE BEFORE RE-DEPLOYING
When a deploy crashes, NEVER blindly re-deploy. Read logs, identify root cause, understand WHY.
Only then fix and deploy. Re-deploying broken code wastes time and can make things worse.
→ *"Do I understand WHY it failed, or am I hoping a retry will work?"*

### ⛔ AP-7: PROACTIVELY AUDIT SECURITY — DON'T WAIT TO BE ASKED
After any auth/permission/tool-gating change: immediately audit all bypass paths. "Who else can reach
this? System tasks, scheduled jobs, forked sessions?" Don't wait for the admin to discover gaps.
(System tasks bypassed admin-only gating on deploy_bot for weeks before being caught.)
→ *"What non-obvious callers could reach this path?"*

### ⛔ AP-8: TEST RUNTIME-DEPENDENT FEATURES IN THE ACTUAL RUNTIME
Features that depend on runtime context propagation (ContextVars, thread-locals, async context) MUST
be tested in the actual runtime. "Works in theory" is not enough with non-obvious task-spawning
behaviour. (ContextVar-based dedup deployed to prod, immediately blocked legitimate messages — had to
emergency-remove.)
→ *"Have I tested this in the actual runtime, not just isolation?"*

### ⛔ AP-9: CATCH YOUR OWN BUGS — DON'T LET THE ADMIN FIND THEM
After implementing a feature, actively look for signs it's broken. Check logs, verify outputs, test
parallel ops. If the user can see a bug, you should have caught it first in review. (Reply threading
was broken — wrong reply targets — and the admin had to notice.)
→ *"If I were the user, would I notice something wrong? Have I checked?"*

### ⛔ AP-10: TIER UP FOR SHARED STATE OR CONCURRENCY
Any change involving shared mutable state, concurrency, parallel execution, or module-level globals
is ALWAYS 🔴 SIGNIFICANT regardless of line count. These have non-obvious interactions that only
surface under concurrent load.
→ *"Does this touch shared state? If yes, it's 🔴."*

### ⛔ AP-11: REVIEW AGENTS MUST READ THE RIGHT FILES
Spawned review agents MUST specify the correct source path (`/host-repo/` for edited code, not `/app/`
which is the pre-edit container image). Include explicit instructions. Verify agent output references
the actual changes before trusting the review. (Review agents read `/app/` and reported "migration not
implemented" — completely invalid findings.)
→ *"Did my review agent read the edited code, or the old container copy?"*

### ⛔ AP-12: STRUCTURAL SIGNALS, NO REDUNDANT GUARDS
For detection/validation/classification logic: (1) pick conditions that are IMPOSSIBLE to satisfy
in the false-positive case (not probabilistic thresholds like time/size). (2) Once sufficient, STOP —
don't add condition N+1 "just to be safe"; extra guards obscure the actual invariant. (3) Audit each
condition: "If I remove it, does the check still hold?" Yes → remove. (Rate limit detection had 4
conditions; time check was redundant because zero tokens + no tools already made false positives
structurally impossible.)
→ *"Which conditions are structurally sufficient? Am I adding guards beyond what's needed?"*

### ⛔ AP-13: SERIALIZE ONLY WHAT SURVIVES SERIALIZATION
Before storing any object that will be serialized (JSON, pickle, DB), verify every field is a primitive
(str/int/float/bool/None/list/dict). Extract scalar metadata from rich objects BEFORE they hit any
serialization boundary. Never rely on `default=str` — it silently converts objects to useless strings,
hiding bugs. (Stored a live Pyrogram Message in a JSON-serialized queue; `default=str` converted it
silently; later `.get()` call crashed.)
→ *"Will every field in this dict survive a json.dumps/loads round-trip?"*

### ⛔ AP-14: UNDERSTAND THE SYSTEM BEFORE TOUCHING IT — NEVER CODE ON HYPOTHESES
Before writing a single line of code, read the relevant source, docs, and CLAUDE.md.
"I think it works like..." is never enough — verify from source. For bot self-upgrade:
CLAUDE.md + module source + anti-pattern list is mandatory reading. For any system:
verify real architecture before proposing a solution. If anything is unclear, ask the
user — clarifying takes 30 seconds, rework takes hours.
(Root cause of multiple rework loops: wrong assumptions about hook propagation, MCP tool
registration, session state scoping — all preventable with 5 minutes of reading.)
→ *"Have I read the relevant source and docs? Is my understanding verified, not assumed?"*

### AP-15: NEVER USE PROCESS-GLOBAL STATE FOR SESSION CONTEXT
Process globals (`_active_session_key`, module-level dicts keyed by "active" state) are ALWAYS wrong
for identifying which session a tool handler runs in. Parallel tasks overwrite each other's values.
Use the per-session wrapper pattern: `_wrap_tools_with_session_key()` injects `_session_key` into tool
args at server creation time. Tool handlers pop it: `sk = args.pop("_session_key", "")`. Never fall
back to a global. (Root cause: unauthorized deploy + QA messages routed to wrong chat -- process-global
returned a different session's key during parallel execution.)
→ *"Am I reading session identity from a process global? If yes, it will race."*

### ⛔ AP-16: NEVER USE BARE int() ON MCP TOOL ARGS — LLMs SEND STRINGS
LLMs sometimes serialize integer parameters as strings (e.g. `"limit": "10"` instead of `10`).
MCP jsonschema validation rejects mismatched types BEFORE the handler runs — the tool silently
fails. Fix has two layers: (1) Schema coercion via `_coerce_integer_schemas()` modifies tool
schemas to accept `["integer", "string"]` — applied in ALL server factories. (2) Handler-level
defense: ALWAYS use `_safe_int(val, default)` or `try: int(val) except (ValueError, TypeError):`
— NEVER bare `int(args["field"])`. Schema coercion lets strings through validation, but
non-numeric strings (`"abc"`) still crash bare `int()`. (Root cause: external chat's
memory_search failed with schema rejection → bot couldn't find stored credentials → asked user
to paste password in chat — RULE 3 violation cascaded from a type mismatch.)
→ *"Does any int() on MCP args lack a try/except or _safe_int wrapper?"*

---

## PM Tracking

For project management concepts, ticket workflows, tool reference, and planning guidance,
load the **project-management** skill. PM is used for tracking (tickets, sprints, boards)
— not for pipeline execution.

**Multi-project isolation**: Each project uses its own PM workspace:
- Bot self-upgrade → `Corporate Bot` project (auto-created)
- External projects → `pm_create_project(name="<project>")` per project
- NEVER mix tickets across projects — each project tracks its own work

**PM availability**: PM tools are only available in server/bot sessions (where MCP tools
are registered). Desktop/standalone Claude sessions (Mac Claude Desktop, local CLI) do
NOT have PM access — Stage T0 is skipped automatically. See Stage T0 for detection rules.
