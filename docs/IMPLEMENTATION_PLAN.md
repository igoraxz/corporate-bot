# RBAC Implementation Plan — Sprint Breakdown

## Sprint R1: RBAC Foundation (no functional changes)

| Ticket | Description | New Files |
|--------|-------------|-----------|
| CB-R1 | `bot/rbac/` package: schema, models, store | 4 files (~350 lines) |
| CB-R2 | PolicyEngine: check_access + caching | 1 file (~250 lines) |
| CB-R3 | Built-in roles: bot_admin, employee | 1 file (~150 lines) |
| CB-R4 | Unit tests for policy evaluation | 1 file (~200 lines) |

## Sprint R2: Domain Knowledge Additions (parallel with R1)

| Ticket | Description | Files Changed |
|--------|-------------|---------------|
| CB-R5 | Add custom_instructions + facts_visible to DomainKnowledge | domains.py |
| CB-R6 | Wire domain instructions into prompt builder | agent.py |

## Sprint R3: Migration + Dual Enforcement

| Ticket | Description | Files Changed |
|--------|-------------|---------------|
| CB-R7 | Harness→RBAC migration script | bot/rbac/seed.py |
| CB-R8 | RBAC subject resolution in session setup | agent.py |
| CB-R9 | RBAC checks in hooks.py (log-only) | hooks.py |
| CB-R10 | Integration tests for dual enforcement | tests/ |

## Sprint R4: RBAC Primary + Harness Slimming

| Ticket | Description | Files Changed |
|--------|-------------|---------------|
| CB-R11 | Feature flag: RBAC_PRIMARY=true | hooks.py, agent.py |
| CB-R12 | Remove 13 security fields from Harness | harness.py |
| CB-R13 | SDK settings from RBAC execution scope | harness.py |
| CB-R14 | Remove bot/core/roles.py (dead code) | roles.py |

## Sprint R5: MCP Tools + Cleanup

| Ticket | Description | Files Changed |
|--------|-------------|---------------|
| CB-R15 | manage_rbac MCP tool | mcp/services.py |
| CB-R16 | Remove legacy hooks layers | hooks.py, tool_labels.py |
| CB-R17 | Documentation update | all docs |
| CB-R18 | Staging regression update | tests/ |

## Estimated Totals

- New code: ~1,180 lines
- Dead code removed: ~415 lines
- Files changed: ~20
- New files: ~8
- Tests: ~300 lines
