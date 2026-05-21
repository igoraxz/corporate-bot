# Knowledge Domains — Full-Stack Architecture

## Overview

Each Knowledge Domain is a composable, self-contained knowledge scope that provides
a complete stack of 9 memory layers. When a harness references multiple domains, all
layers are composed (joined) together, creating a unified knowledge environment.

## Domain Data Model

### Domain Registry (extended schema)

```sql
domain_registry (
    domain_id TEXT PRIMARY KEY,        -- lowercase alphanumeric + hyphens (e.g. 'eng-runbooks')
    name TEXT NOT NULL,                -- human-readable name
    description TEXT NOT NULL,         -- brief description for AI routing decisions
    owner TEXT NOT NULL DEFAULT '',    -- owner email/team
    config_yaml TEXT NOT NULL,         -- full YAML config (source + indexing + access + knowledge)
    
    -- Knowledge stack config (denormalized from YAML for fast queries)
    prompt_modules TEXT DEFAULT '[]',      -- JSON array: module names for system prompt
    skills TEXT DEFAULT '[]',             -- JSON array: relevant skill names
    auto_load_skills TEXT DEFAULT '[]',   -- JSON array: skills that pre-load in CLAUDE.md
    facts_categories TEXT DEFAULT '[]',   -- JSON array: fact categories owned by this domain
    
    -- Hot context (cached domain summaries for prompt injection)
    hot_summary TEXT DEFAULT '',
    hot_summary_updated_at TEXT DEFAULT '',
    
    -- Runtime state
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

### Domain Config YAML (extended)

```yaml
domain:
  id: eng-runbooks
  name: Engineering Runbooks
  description: >
    Deployment guides, operational runbooks, incident response procedures,
    and infrastructure documentation for the engineering team.
    Contains: deploy pipelines, monitoring setup, database operations,
    scaling procedures, security protocols.
    
  owner: platform-team@company.com
  
  source:
    type: github                    # github | graph_api | local
    repo: org/eng-runbooks
    branch: main
    # Graph API fields (for type: graph_api):
    # site_id: "host,guid1,guid2"  # SharePoint site ID
    # drive_id: "..."              # Drive ID (optional, resolved from site)
    # team_id: "..."               # Teams team ID
    # channel_id: "..."            # Teams channel ID
    paths: ["docs/", "runbooks/"]
    exclude: ["drafts/"]
  
  indexing:
    chunking_strategy: markdown_headers
    chunk_size: 1500
    chunk_overlap: 200
    poll_interval_minutes: 5
  
  access:
    teams: ["engineering", "sre", "devops"]
    admins: ["lead@company.com", "cto@company.com"]
  
  # NEW: Full knowledge stack configuration
  knowledge:
    prompt_modules:          # Layer 5: system prompt context
      - deploy
      - staging
      - pitfalls
    skills:                  # Layer 7: relevant capabilities
      - browser-automation
      - coding-principles
    auto_load_skills:        # Layer 8: pre-loaded into CLAUDE.md
      - coding-principles
    facts_categories:        # Layer 9: fact categories owned
      - infrastructure
      - deploy
      - monitoring
    essential_docs:          # Always summarized in hot context
      - deploy/pipeline.md
      - ops/runbook-template.md
    hot_context_budget: 3000 # Max chars for this domain's hot summary
```

## The 9 Layers

### Layer 1: Chat Histories
**Decision: Chat-scoped (NOT domain-scoped)**

Messages are conversational artifacts, not domain knowledge. A question like
"help me deploy the new feature" belongs to the chat context, not to the
"eng-runbooks" domain. Domain value comes from curated docs (L3-4) and
structured facts (L9).

No changes needed — existing chat_id scoping via AccessContext works.

### Layer 2: RAG on Chat Histories  
**Decision: Chat-scoped (NOT domain-scoped)**

Same rationale as Layer 1. Existing rag_search() with chat_id filtering stays.

### Layer 3: Documentation
**Status: DONE (Sprints 7, 15, 16)**

domain_documents table + rag_chunks with domain_id. Three source types:
- **GitHub** (`github_sync.py`): Webhook + polling, SHA-based content-hash gating
- **Graph API** (`graph_sync.py`): SharePoint sites, Teams channel files, OneDrive for Business via Microsoft Graph API. Delta query support for incremental sync. Requires `Files.Read.All` + `Sites.Read.All`.
- **Local filesystem**: Direct file indexing

Document parsing via `doc_parser.py` with Gemini Flash multimodal extraction for PDF, DOCX, XLSX, PPTX, and images. Fallback to pypdf/python-docx when Gemini is unavailable.

Contextual chunk enrichment (Anthropic Contextual Retrieval pattern): Gemini Flash generates a one-line context prefix for each chunk at indexing time, improving retrieval quality by ~49%.

### Layer 4: RAG on Documentation
**Status: DONE (Sprint 7)**

search_domains() with domain_id filtering in WHERE clause. Two-tier knowledge architecture:
- **Hot context** (Sprint 14): Gemini-generated knowledge maps cached in `domain_registry.hot_summary`, injected into system prompt for immediate domain awareness.
- **Cold context**: Full RAG search on demand via `search_knowledge()` tool.

### Layer 5: System Prompt Modules
**Implementation: Per-domain `prompt_modules` list, composed via harness**

Each domain declares which modules should be injected into the system prompt.
When a harness references domains D1, D2, D3:

```
composed_modules = dedupe(
    harness.claude_md_modules +     # harness's own modules
    D1.prompt_modules +              # domain 1's modules
    D2.prompt_modules +              # domain 2's modules  
    D3.prompt_modules                # domain 3's modules
)
```

New helper: `get_composed_modules(harness, domain_ids) -> list[str]`

### Layer 6: Skills Awareness
**Implementation: Domain descriptions in prompt + per-domain skill relevance**

Each domain's `description` field is injected into the system prompt so the model
can assess which domain is relevant for a query. The `skills` list tells the model
which capabilities are useful within this domain.

New prompt section: "ACTIVE KNOWLEDGE DOMAINS:" with id, name, description,
and relevant skills for each domain accessible in the current session.

### Layer 7: Skills
**Implementation: Skills are global; domains declare relevance**

All skills remain globally available (SDK auto-discovers from .claude/skills/).
Domains don't restrict which skills exist — they declare which skills are
*relevant* to help the model's skill assessment.

### Layer 8: Skill Auto-Load Settings
**Implementation: Per-domain `auto_load_skills` composed into CLAUDE.md**

```
auto_load = dedupe(
    harness.claude_md_modules +
    D1.auto_load_skills +
    D2.auto_load_skills +
    D3.auto_load_skills
)
```

These get composed into CLAUDE.md or inline-injected, following the existing
module loading pattern.

### Layer 9: Facts
**Implementation: Domain-scoped facts via domain_id column + model-driven routing**

Add `domain_id` column to `message_facts` table. When the model creates a fact
in a multi-domain chat, it decides which domain(s) to tag it with based on the
domain descriptions.

**Model-driven write routing:**
The model sees all active domain descriptions in its prompt. When it emits
FACTS_UPDATE, it includes a `domain` field:

```
FACTS_UPDATE: {"deploy_pipeline_url": {
    "value": "https://ci.company.com/pipelines",
    "category": "infrastructure",
    "domain": "eng-runbooks"
}}
```

If no domain specified, fact is chat-local (domain_id = '').
If multiple domains relevant, model can emit the same fact multiple times
with different domain tags.

**Fact retrieval composition:**
```sql
-- Facts for prompt = union of:
-- 1. Chat-local facts (domain_id = '')  
-- 2. Facts from all domains in harness.domains
WHERE expired = 0
  AND (domain_id = '' OR domain_id IN (...allowed_domains...))
  AND source_chat_id filter (existing)
  AND category filter (existing)
```

## Composition Flow

### Session Setup (agent.py)

```python
# 1. Resolve harness for this chat
harness = get_harness(session_key)

# 2. Get domain configs for all referenced domains
domain_ids = harness.domains or []
domain_configs = await get_domain_knowledge_configs(domain_ids)

# 3. Compose modules (harness + domains) via compose_effective_modules()
composition = compose_effective_modules(domain_configs, harness_filter=harness.claude_md_modules)

# 4. Compose fact categories (harness + domains)  
composed_categories = compose_domain_categories(harness, domain_configs)

# 5. Build access context with domain access
access_ctx = AccessContext(
    ...,
    allowed_domains=tuple(domain_ids),
)

# 6. Build system prompt with domain awareness
system_prompt = build_system_prompt(
    ...,
    claude_md_modules=composed_modules,
    active_domains=domain_configs,  # NEW: for domain descriptions in prompt
)

# 7. Get facts with composed categories + domain scoping
facts = get_facts_for_prompt(
    access_ctx=access_ctx,
    categories=composed_categories,
)
```

### Domain Awareness Prompt (new section injected into system prompt)

```
ACTIVE KNOWLEDGE DOMAINS:
Your session has access to these knowledge domains. Use their descriptions
to decide where to route queries (search_knowledge) and where to store
new facts (FACTS_UPDATE with domain field).

[eng-runbooks] Engineering Runbooks
  Deployment guides, operational runbooks, incident response procedures.
  Skills: browser-automation, coding-principles
  
[hr-policies] HR Policies & Guidelines
  Employee handbook, leave policies, performance review processes.
  Skills: email-operations

When storing facts, choose the most relevant domain based on content.
If a fact spans multiple domains, store it in each relevant domain.
If no domain is relevant, omit the domain field (fact stays chat-local).
```

## Migration Plan

### Phase 1 (CB-42): Schema + Config
1. ALTER TABLE domain_registry ADD COLUMN prompt_modules/skills/auto_load_skills/facts_categories
2. Extend DomainConfig with knowledge: section parsing
3. Denormalize knowledge fields into columns on register_domain()
4. Add get_domain_knowledge_configs() helper
5. Update manage_domain tool to show new fields

### Phase 2 (CB-43): Composition  
1. Implement compose_effective_modules() and compose_domain_categories()
2. Wire into agent.py session setup (build composed modules + categories)
3. Wire into prompts.py (domain awareness section)
4. Wire into harness.py (compose CLAUDE.md with domain modules)
5. Wire into facts retrieval (domain-scoped get_facts_for_prompt)

### Phase 3 (CB-44): Domain-Scoped Facts
1. ALTER TABLE message_facts ADD COLUMN domain_id
2. Extend FACTS_UPDATE regex to parse domain field
3. Wire domain_id through save_message_fact()
4. Update _domain_sql_filter() usage in fact queries
5. Update fact prompts to guide model on domain routing

### Phase 4 (CB-45): Unified Search
1. Merge memory_search + search_knowledge result presentation
2. Domain-aware search ranking
3. RAG stats per domain
4. Performance optimization for multi-domain search

## Design Decisions

1. **Messages stay chat-scoped** — not domain artifacts
2. **Domains are flat** (not hierarchical) — simpler to compose, hierarchies add complexity without clear benefit
3. **Model decides write routing** — not static harness config; model sees domain descriptions and chooses
4. **Facts can be multi-domain** — same fact stored with multiple domain_ids if relevant to multiple
5. **Skills are global** — domains declare relevance, not exclusivity
6. **Prompt budget** — domain descriptions are brief (~100 chars each), domain awareness section ~1KB for 5 domains. Well within 120KB limit.
7. **Backward compat** — existing chats without domains continue to work (empty domains = no domain features)
