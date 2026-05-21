---
description: "Project management: tickets, sprints, kanban board, task tracking, PM tools, pipeline vs direct coding, budget awareness."
---

# Project Management — Tickets, Sprints & Task Tracking

The bot includes a built-in project management system (PM Agent Bus) for structured task
execution. Tickets are useful in ANY workflow — not just the automated pipeline — for
tracking work items, managing TODOs, and organizing multi-step tasks.

## Core Concepts

- **Project**: Top-level container. One per workspace/initiative. Holds sprints and tickets.
- **Sprint**: A batch of tickets to execute together. Statuses: draft → approved → running → completed.
- **Ticket**: A single work item (bug fix, feature, refactor). The atomic unit of work.
  Statuses: backlog → ready → in_progress → review → done (or dropped).
- **TCD (Ticket Context Document)**: Structured checkpoint for crash recovery and state tracking during ticket execution.
  Contains objective, plan, files modified, findings, test results, budget, stage_costs.

## Ticket Lifecycle

```
backlog → ready → in_progress → review → done
                                  ↓
                               dropped
```

- **backlog**: Captured but not prioritized. Use for ideas, future work, deferred items.
- **ready**: Prioritized and scoped. Clear description, acceptance criteria defined.
- **in_progress**: Actively being worked on (manually or by pipeline agent).
- **review**: Implementation complete, awaiting verification.
- **done**: Verified and completed.
- **dropped**: Cancelled or no longer relevant.

Tiers determine review depth:
- **small**: ≤3 files, single module, clear requirements (bug fix, config, helper)
- **medium**: 2-5 files, cross-module within same subsystem, moderate complexity
- **significant**: ≥5 files, cross-subsystem, security/auth-sensitive

Priority (lower number = higher priority): controls execution order within a sprint.

## PM Tool Reference

| Tool | Purpose |
|------|---------|
| `pm_create_project` | Create a project for a workspace |
| `pm_create_ticket` | Add a work item (title, description, tier, priority) |
| `pm_move_ticket` | Transition ticket status (e.g. ready → in_progress → done) |
| `pm_board` | View all tickets by status (kanban board) |
| `pm_list_tickets` | List tickets filtered by sprint_id, project_id, parent_ticket_id, and/or status |
| `pm_list_projects` | List all projects |
| `pm_update_ticket` | Update ticket fields + sprint reassignment (sprint_id param triggers atomic budget resync) |
| `pm_reset_ticket` | Reset a stuck ticket back to ready |
| `pm_create_sprint` | Group tickets into a sprint batch |
| `pm_transition_sprint` | Move sprint between statuses |
| `pm_plan` | Quick AI planning without full pipeline execution |
| `pm_auto_ticket` | **PM-first workflow entry point** — auto-creates project + rolling sprint + ticket in one call |
| `pm_budget_report` | Cost tracking across project/sprint/ticket |
| `pm_retro` | Sprint retrospective (cost, velocity, lessons) |
| `pm_summary` | Sprint summary for demo/reporting |
| `pm_get_tcd` | Retrieve Ticket Context Document |

## PM-First Coding Workflow (Mandatory)

**Every coding task creates a PM ticket before implementation.** This is enforced by
Stage T0 in the coding-principles pipeline.

### How it works:
1. `pm_auto_ticket(title, tier, operator_request)` — creates project + rolling sprint + ticket
2. Work on the task, committing with `Ticket: PM-<id>` git trailer
3. `pm_update_ticket(ticket_id, commit_hash="<sha>")` — links commit to ticket
4. `pm_move_ticket(ticket_id, to_status="done")` — closes the ticket
5. On deploy, `deploy_bot` auto-parses Ticket: trailers → links tickets to deploys

### Rolling sprints:
- Monthly sprints are auto-created: "2026-04 Rolling"
- No manual sprint management needed for ad-hoc work
- Full sprints (pm_create_sprint) still available for planned batches

### Sprint Placement Policy (FB Project)

Tickets are placed in sprints based on their tier:

| Tier | Initial Sprint | Escalation Trigger | Action |
|------|---------------|-------------------|--------|
| Small | Rolling | 2+ follow-up tickets from PO | Create individual sprint for follow-ups |
| Medium | Rolling | 2+ follow-up tickets from PO | Create individual sprint for follow-ups |
| Significant | Individual (always) | N/A (already isolated) | Follow-ups join same sprint |

**Follow-up tickets** (from PO feedback during/after implementation or after deploy):
- Always linked via `parent_ticket_id`
- Tiered independently from parent
- Placed per escalation rules above

**Individual sprints**: Named after the task (e.g. "Implement relay file attachments").
Goal captures full scope including accumulated follow-ups.


### Commit trailers:
Use git trailers (at end of commit message body, after blank line):
```
bot-self-upgrade: add PM-first workflow

Ticket: PM-42
Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
```

### Deploy linkage:
- deploy_bot auto-parses `Ticket: PM-<id>` from git log
- Creates pm_deploys record with commit range
- Links all referenced tickets via pm_deploy_tickets join table
- Enables full traceability: operator request → ticket → commit → deploy

### Parent-child tickets:
Use `parent_ticket_id` to link bugs/subtasks to a parent:
- Bug found during task PM-42 → `pm_auto_ticket(..., parent_ticket_id=42)`
- Child tickets show up in `pm_board` and retros

## Using Tickets for Task Tracking

You don\'t need the automated pipeline to benefit from tickets. Use them as a structured
task tracker:

1. **Create a project** for the workspace: `pm_create_project`
2. **Create tickets** for each task: `pm_create_ticket` with clear title, description, and tier
3. **Track progress** by moving tickets: `pm_move_ticket` (backlog → ready → in_progress → done)
4. **View the board**: `pm_board` shows all tickets grouped by status (kanban view)
5. **Group related work**: `pm_create_sprint` to batch tickets for ordered execution

### When to Use Tickets vs Simple TODO Facts

| Scenario | Approach |
|----------|----------|
| Quick reminder, one-off task | TODO fact (simple, lightweight) |
| Multi-step project with tracking | PM tickets (structured, status tracking) |
| Need audit trail / cost tracking | PM tickets (budget cascade built in) |
| Recurring or template-based work | PM tickets + sprint batching |
| Personal tasks, appointments | TODO facts (personal, date-based) |
| Software development tasks | PM tickets (tier, priority, pipeline support) |

## Coding Pipeline Integration

PM tickets integrate with the coding-principles review pipeline:

1. **T0** — `pm_auto_ticket` creates ticket before any work
2. **Coding** — Implementation follows the tier-appropriate review pipeline (SMALL/MEDIUM/SIGNIFICANT)
3. **Commit** — Git trailer `Ticket: PM-<id>` links commits to tickets
4. **Done** — `pm_move_ticket` closes the ticket

The review pipeline (agents, plan, etc.) is defined by coding-principles, not by PM.
PM provides tracking, audit trail, and budget accounting.

### When to Use Tickets

| Scenario | Approach |
|----------|----------|
| Quick fix, single file | PM ticket (SMALL tier) + direct coding |
| Bug fix, clear scope | PM ticket + coding pipeline |
| Multi-file feature | PM ticket (MEDIUM/SIGNIFICANT) + coding pipeline |
| Multiple related changes | Sprint with ordered tickets |
| Need audit trail / cost tracking | Always use tickets |

## Budget Awareness

Every automated agent dispatch costs $3-5 (model inference). Fix iterations multiply this.
Budget cascades atomically: agent_runs → tickets → sprints → projects.
Use `pm_budget_report` to track spend at any level.
Write quality code on the first pass — each fix loop burns budget.

## Access

PM tools require explicit harness configuration (`allowed_tools`) for non-admin users.
Admin users have full access to all PM tools in any chat.
For harness setup, ask an admin to add PM tool prefixes to the chat\'s harness.
