# Component: PM Agent Bus (v5 In-Session)

**Purpose**: Structured ticket/sprint execution. The calling session IS the PM+Engineer.
No separate agent sessions. Review via security-reviewer + quality-reviewer agents only.

**Key files**: `bot/pm/` (db.py, schema.py, store.py, tools.py, budget.py, tcd.py, orchestrator.py)

---

## Design Principles

- **In-session execution** — the calling chat session IS the PM + Engineer. No pm-engineer / pm-verifier agents.
- **TCD (Ticket Context Document)** — checkpoint per phase for crash recovery (not agent handoff)
- **Bounded fix loops** — CRITICAL findings halt; HIGH max 2 fix iterations; MEDIUM first pass only; 3 total cap per ticket
- **Own SQLite DB** — `pm.db` (WAL + FK enforcement), separate from `conversations.db`
- **Budget cascade** — atomic sync: agent_runs → tickets → sprints → projects in one transaction
- **Concurrency guards** — per-ticket and per-sprint `asyncio.Lock` via `setdefault()` to prevent parallel budget corruption
- **Rolling sprints** — `pm_create_sprint` auto-closes active sprints in the same project (single active per project)
- **Deploy linkage** — tickets track `commit_hash`; `find_tickets_by_commits` uses LIKE with escaped wildcards
- **Harness-gated** — PM tools blocked at hooks Layer 1b2; non-admin coding chats need explicit `allowed_tools` containing the PM MCP prefix

## Ticket Execution Protocol

```
pm_run_ticket(ticket_id) →
  Phase 0: crash recovery check (read TCD.current_state)
  Phase 1: Plan (Subject Matter Research FIRST → design → acceptance criteria → TCD update)
  Phase 2: Implement (code + syntax check + docs update → TCD update)
  Phase 3: Review (security-reviewer + quality-reviewer agents → fix loop → TCD update)
  Phase 4: Complete (pm_move_ticket → done → TCD final update)
```

## TCD current_state Values

```
"created"              → start from Phase 1
"planned"              → skip to Phase 2
"implemented"          → skip to Phase 3
"verified (approve*)"  → skip to Phase 4
"done"                 → skip entirely
```

## Data Model

```
Project
  └── Sprint (status: planning / active / completed / cancelled)
        └── Ticket (priority-ordered)
              ├── agent_runs (cost tracking)
              └── TCD checkpoint (current_state + plan + files + findings + costs)
```

## Key Tools

```python
pm_create_ticket(title, description, sprint_id)
pm_run_ticket(ticket_id)         # triggers in-session execution protocol
pm_run_sprint(sprint_id)         # runs all tickets in priority order
pm_update_ticket(ticket_id, tcd=...)   # MUST be called after EVERY phase
pm_move_ticket(ticket_id, to_status)  # status transitions
pm_board()                       # kanban view of all projects
pm_retro(sprint_id)              # post-sprint cost/velocity/findings analysis
pm_summary(sprint_id)            # compact demo digest for reporting
```

## Anti-Patterns

- **NEVER** spawn pm-engineer or pm-verifier agents (v5 is in-session only)
- **NEVER** skip TCD phase updates — crash recovery depends on `current_state` in TCD
- **NEVER** use pm.db for general bot memory — PM-only DB, separate schema
- **NEVER** reference `/host-repo` in ticket descriptions for staging tasks — read-only on staging
- **NEVER** run sprints without `query_timeout_s=0` in harness — zombie detector kills 20-40 min tasks

## Pitfalls

- **Long runtime** — full sprint (plan + implement + review with fix loops) takes 15-60 min.
  Set `query_timeout_s=0` in harness (default 900s kills long-running tasks). This is a hard requirement.
- **Staging /test/inject** — PM tasks need timeout=3600 + admin-level access in the inject call.
  Without these, the task is killed partway through.
- **Staging harnesses differ from prod** — harnesses.json is per Docker volume. Use `harness_config`
  in `/test/inject` to auto-create the required harness on staging.
- **Integer schema coercion** — PM MCP server coerces ticket_id/sprint_id to int at entry point.
  The coercion in `create_pm_server()` handles SDK string→int drift. Don't remove it.
- **Budget cascade atomicity** — `sync_budget_chain()` runs in one transaction.
  Per-ticket `_ticket_locks` prevent race conditions. Never bypass these locks.
- **Sprint tickets processed sequentially** — not parallel, to prevent DB lock contention.
  This is by design, not a bug.
- **Lock dict pattern** — `_project_locks.setdefault(key, asyncio.Lock())` is atomic at dict level;
  `setdefault` avoids the TOCTOU race of `if key not in dict: dict[key] = Lock()`.
- **LIKE injection in `find_tickets_by_commits`** — SHA prefix is escaped (`%` → `\%`, `_` → `\_`)
  with `ESCAPE '\\'` clause. Never pass user strings directly into LIKE patterns.
- **Scaffolding functions** — `get_deploy`, `find_tickets_by_commits`, `get_child_tickets`,
  `get_orphan_tickets` exist for upcoming tools. Keep with TODO comments until callers are added.
