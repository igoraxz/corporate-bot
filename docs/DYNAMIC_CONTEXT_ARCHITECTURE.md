# Dynamic Context Filling — Architecture Design

## Problem Statement

The bot has access to knowledge domains (document collections from GitHub, SharePoint, and Teams
indexed via RAG). Without proactive context, domain knowledge is accessed reactively — the model
calls `search_knowledge` after seeing a user query. This misses opportunities for proactive context:

1. **The model doesn't know what it doesn't know** — without seeing domain summaries in its prompt,
   it may not realize relevant documentation exists
2. **First-query cold start** — the first question about a domain gets worse answers because
   the model has no domain context yet
3. **Important facts buried in RAG** — high-value information (deploy procedures, critical configs)
   should be immediately available, not behind a search tool call
4. **No query-aware context selection** — the same static prompt is used regardless of query topic

## Solution: Two-Tier Knowledge Architecture

### Tier 1: Hot Context (Always in Prompt)

Domain summaries and key facts injected into the system prompt or CLAUDE.md. This is the
"awareness layer" — enough context for the model to:
- Know what knowledge exists in each domain
- Answer common questions without tool calls
- Make intelligent routing decisions for search and fact storage

**Budget**: ~2-5KB per domain (domain description + key facts + cross-references)
**Total**: ~10-25KB for 5 domains (well within 120KB prompt limit)

**Content selection criteria** (automatic):
- Document titles and section headings (structural overview)
- Most-retrieved chunks (usage frequency tracking)
- Most recently updated content (recency signal)
- Documents flagged as "essential" in domain config
- Key facts tagged with this domain

**Update trigger**: On domain re-sync (Git webhook or poll)

### Tier 2: Cold Context (On-Demand via RAG)

Full document chunks stored in the RAG system, retrieved via `search_knowledge`.
Hot context entries include cross-references:
```
[eng-runbooks] Engineering Runbooks
  Key topics: deploy pipeline, monitoring setup, incident response
  Essential docs: deploy/pipeline.md, ops/monitoring.md, incident/playbook.md
  → Use search_knowledge(query, domain_filter="eng-runbooks") for full details
```

The model sees the summary (Tier 1) and knows to search for details (Tier 2).

## Implementation Design

### Component 1: Domain Summary Generator (DONE)

Module: `bot/storage/domain_summary.py`

Two-tier summary generation:
1. **Gemini-powered knowledge maps** (primary): Uses Gemini Flash to generate compressed
   topic summaries with TOPICS / KEY INFORMATION / DIVE DEEPER structure. Reads domain metadata,
   document inventory, key facts, and chunk previews as input.
2. **File-list fallback**: When Gemini is unavailable, builds a simple summary from
   domain description + document inventory + key facts.

Summaries cached in `domain_registry.hot_summary` column. Regenerated on domain sync
or manual `manage_domain(action="refresh_summary")`.

### Component 2: Domain Awareness Prompt Injection (DONE)

Enhancement to `bot/prompts.py:build_system_prompt()`:
- `build_domain_awareness_prompt()` in `domains.py` generates the "ACTIVE KNOWLEDGE DOMAINS"
  prompt section with domain descriptions, relevant skills, and hot summaries.
- Hot summaries are fetched via `get_domain_hot_summaries()` during session setup in `agent.py`.
- Auto-generates missing summaries on first access.

**Note**: Pre-query context selection (per-query dynamic injection) was evaluated and dropped
(Phase C) -- the static hot summaries provide sufficient domain awareness with zero query-time latency.

### Component 3: Usage Tracking

Track which domain chunks get retrieved most often to improve hot context selection:

```sql
-- New column on rag_chunks
retrieval_count INTEGER DEFAULT 0,
last_retrieved_at TEXT

-- Updated on each search_knowledge result
UPDATE rag_chunks SET retrieval_count = retrieval_count + 1,
                      last_retrieved_at = ? WHERE rowid IN (...)
```

### Component 4: Essential Documents

New field in domain config YAML:

```yaml
knowledge:
  essential_docs:           # Always summarized in hot context
    - deploy/pipeline.md    # Deploy procedure (most queried)
    - ops/runbook-template.md
  hot_context_budget: 3000  # Max chars for this domain's hot summary
```

### Component 5: Domain Summary Cache

Summaries are expensive to generate (requires reading chunks, ranking, formatting).
Cache in the domain_registry table:

```sql
ALTER TABLE domain_registry ADD COLUMN hot_summary TEXT DEFAULT '';
ALTER TABLE domain_registry ADD COLUMN hot_summary_updated_at TEXT DEFAULT '';
```

Regenerated on:
- Domain re-sync (new/changed documents)
- Manual `manage_domain(action="refresh_summary")`
- Scheduled refresh (hourly or daily)

## Teams/SharePoint Document Indexing

### Microsoft Graph API Integration

Additional data sources beyond GitHub:

1. **SharePoint Document Libraries** — Teams channel files are stored in SharePoint
   - API: `GET /sites/{site-id}/drives/{drive-id}/root/children`
   - Permissions: `Sites.Read.All` or `Files.Read.All`
   - Change notifications: `POST /subscriptions` for file changes

2. **OneDrive for Business** — Personal employee files (shared folders)
   - API: `GET /users/{user-id}/drive/root/children`
   - Scoped per employee (privacy-preserving)

3. **Teams Chat Files** — Files shared in chat messages
   - API: `GET /chats/{chat-id}/messages` → extract attachments
   - Permissions: `Chat.Read` or `ChannelMessage.Read.All`

### Implementation: Graph API Document Source

Extend `DomainSource` to support Graph API:

```yaml
source:
  type: graph_api          # NEW: Microsoft Graph API source
  site_id: "..."          # SharePoint site ID
  drive_id: "..."         # Document library drive ID
  paths: ["General/Docs"] # Folder paths within the library
  poll_interval_minutes: 15
```

New sync module: `bot/storage/graph_sync.py` (mirrors `github_sync.py` pattern):
- Uses MS Graph API (via existing Softeria MCP or direct httpx calls)
- Downloads files from SharePoint/OneDrive
- Passes to existing `doc_parser.py` → `index_document()` pipeline

## Cognee Integration Assessment

### What Cognee Provides
- Knowledge graph construction from documents (entity extraction + relationships)
- Graph-augmented retrieval (GraphRAG pattern)
- Multi-hop reasoning over connected entities
- Python SDK, open source (Apache 2.0)

### Assessment for Our System
- **Pro**: Knowledge graphs would improve cross-document retrieval (e.g., "who is responsible
  for the deploy pipeline?" requires entity extraction + relationship mapping)
- **Pro**: Built-in importance scoring via graph centrality metrics
- **Con**: Additional dependency (Neo4j or in-memory graph)
- **Con**: Our SQLite + cosine similarity approach works well for document retrieval
- **Con**: Graph construction is compute-intensive (LLM calls for entity extraction)

### Recommendation: Phase 2 Integration
Start with the simpler two-tier approach (hot summaries + cold RAG). If cross-document
reasoning proves insufficient, add Cognee as an optional graph layer:

```python
# bot/storage/graph_layer.py (future)
# Optional: pip install cognee
# Builds knowledge graph from domain documents
# Enhances search_knowledge with graph traversal
```

## Implementation Phases

### Phase A: Hot Context Summaries -- DONE (Sprint 14)
1. Added `hot_summary` and `hot_summary_updated_at` columns to domain_registry
2. Implemented `generate_domain_summary()` with Gemini-powered knowledge maps
3. Inject hot summaries into domain awareness prompt section via `build_domain_awareness_prompt()`
4. Added `essential_docs` and `hot_context_budget` to domain config YAML
5. Summaries regenerated on domain sync, manual refresh via `manage_domain(action="refresh_summary")`

### Phase B: Contextual Chunk Enrichment -- DONE (Sprint 15)
Original plan was usage-aware ranking. Pivoted to Anthropic Contextual Retrieval pattern
which provides higher quality improvement at indexing time:
1. Gemini Flash generates a one-line context prefix per chunk at indexing time
2. Context prefix situates the chunk within its source document and domain
3. Enriched chunks stored directly in rag_chunks (no runtime overhead)
4. ~49% retrieval quality improvement per Anthropic benchmarks

### Phase C: Pre-Query Context Selection -- DROPPED
Analysis showed diminishing returns vs complexity:
- Hot summaries (Phase A) already give the model domain awareness
- Query-time embedding comparison adds 200-500ms latency per query
- Benefit is marginal when hot summaries are well-structured knowledge maps
- Cost of maintaining query-domain routing logic outweighs quality gains

### Phase D: Graph API Document Sources -- DONE (Sprint 16)
1. Extended DomainSource for `graph_api` type with `site_id`, `drive_id`, `team_id`, `channel_id`
2. Implemented `graph_sync.py` (SharePoint sites, Teams channel files, OneDrive for Business)
3. Delta query support for incremental sync
4. SSRF prevention: URL validation ensures all requests target `graph.microsoft.com`
5. Reuses existing `doc_parser.py` + `index_document()` pipeline

### Phase E: Knowledge Graph -- FUTURE (not started)
Deferred -- current two-tier architecture (hot summaries + contextual enrichment + RAG)
provides strong cross-document retrieval. Graph layer will be evaluated if
multi-hop reasoning proves insufficient for complex queries.

## Trade-offs

| Approach | Latency | Quality | Cost | Complexity | Status |
|----------|---------|---------|------|------------|--------|
| Reactive search only | 0ms | Medium | Low | Low | Baseline |
| + Hot summaries (Phase A) | +50ms | High | Low | Medium | DONE |
| + Contextual enrichment (Phase B) | +0ms (index-time) | Higher | Low | Medium | DONE |
| + Pre-query selection (Phase C) | +200-500ms | Marginal gain | Medium | High | DROPPED |
| + Graph API sources (Phase D) | +0ms | Higher coverage | Medium | High | DONE |
| + Knowledge graph (Phase E) | +500ms | Best reasoning | High | Very High | FUTURE |

**Current architecture**: Phases A + B + D provide the best quality-to-complexity ratio.
The model gets immediate domain awareness (hot summaries), high-quality retrieval (contextual
enrichment), and broad document coverage (GitHub + Graph API sources).
