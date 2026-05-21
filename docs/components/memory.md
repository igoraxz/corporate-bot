# Component: Memory System (Messages, Facts, RAG, Media)

**Purpose**: Persistent storage and retrieval across SQLite (conversations.db) combining
full-text search, semantic RAG, structured facts (graph triples), and media caching.

**Key files**: `bot/storage/memory.py`, `bot/storage/facts.py`, `bot/storage/media_cache.py`, `bot/storage/rag.py`, `bot/storage/db.py`

---

## Design Principles

- **Single shared SQLite DB** (conversations.db) with WAL mode + busy_timeout
- **AccessContext** — frozen dataclass with `allowed_chat_ids`; enforced at SQL layer (`WHERE chat_id IN (...)`)
  - Team chats: access to all `ORG_CHAT_IDS`
  - External chats: access ONLY to their own `chat_id` (security-critical isolation)
- **Zero truncation** — no `[:N]` caps anywhere on stored messages, bot responses, or phone transcripts
- **FACTS_UPDATE from ALL turns** — `all_text_blocks` accumulates every TextBlock from every SDK turn;
  fact extraction runs on all of them, not just `final_text`
- **Version chaining** — old facts marked `expired=1`, `superseded_by` links to new fact ID; full audit trail

## Database Tables

| Table | Purpose |
|-------|---------|
| `messages` | All conversation messages (source, user, text, role, timestamp, msg_type) |
| `message_facts` | Graph triples: subject / fact_key / fact_value + category, doc_pointer, expiry |
| `rag_chunks` | Semantic chunks with 3072-dim Gemini embeddings (float32 blobs) |
| `media_cache` | Cached media files with AI descriptions, tg_file_id for re-fetch |
| `session_map` | SDK session persistence: session_key → session_id |
| `messages_fts` | FTS5 index on messages (text, user_name) |
| `message_facts_fts` | FTS5 index on facts (subject, key, value, category) |
| `media_cache_fts` | FTS5 index on media (filename, description, sender — content-searchable) |

## Search Tools

| Tool | When to use |
|------|------------|
| `memory_search` | PRIMARY — runs FTS5 + facts + RAG + media in parallel, one call |
| `search_facts` | Browse/filter all facts by category/subject |
| `get_messages_by_range` | Zoom into msg ID range or date range after memory_search |
| `get_message_context` | Expand context around a single message |
| `search_media` | Browse/filter media by type or content description |

## Anti-Patterns

- **NEVER allow admin AccessContext to leak to non-admin sessions — security-critical data isolation
- **NEVER** set AccessContext lazily — set it BEFORE the query via `set_access_context(session_key, ctx)`
- **NEVER** emit FACTS_UPDATE only in intermediate SDK turns if only `final_text` is extracted — emit in FINAL response
- **NEVER** use `[:N]` truncation on stored messages — zero truncation policy
- **NEVER** consolidate timing-critical data (flight times, appointments) into summary strings — store as atomic facts
- **NEVER** hard-delete facts — mark as `expired=1` with version chain

## Pitfalls

- **FACTS_UPDATE in intermediate turns** — if model emits FACTS_UPDATE before a tool call, it ends up in an
  intermediate TextBlock, not `final_text`. Fixed by `all_text_blocks` accumulation in agent.py.
  Never rely on `final_text` alone for fact extraction.
- **AccessContext is security-critical** — wrong `allowed_chat_ids` = cross-chat data leak.
  Scheduled tasks with empty chat_id get admin access (built-in tasks are designed this way intentionally).
- **DB singleton** — use `await get_db()` not the module-level var directly; lazy-init lock prevents concurrent creation.
- **RAG chunks have chat_id** — per-chat isolated. Full rebuild via `rag_backfill` tool; incremental on new messages.
- **FTS5 UPDATE trigger** on `media_cache` auto-indexes new descriptions as they arrive from Gemini async generation.

## AccessContext Construction

```python
# Team chat — access to all team data:
ctx = AccessContext(allowed_chat_ids=frozenset(config.ORG_CHAT_IDS))

# External chat — isolated to own chat only:
ctx = AccessContext(allowed_chat_ids=frozenset({chat_id}))

# Admin / built-in scheduled tasks — all data:
ctx = AccessContext(allowed_chat_ids=frozenset())  # empty = no WHERE filter = all data
```

## Fact Graph Schema

```
subject:     auto-derived from key prefix (e.g. "john_passport" → subject="john")
fact_key:    snake_case key (e.g. "john_passport_expiry")
fact_value:  string value
category:    free-form (documents, travel, school, bot, credentials, todo, ...)
doc_pointer: optional path to linked document (passport scan, booking PDF, etc.)
expired:     0 = active | 1 = superseded or manually expired
superseded_by: ID of newer fact (version chaining for audit trail)
```

## RAG Architecture

- **Chunking**: size-aware character-budget (2000 chars/chunk, max 7 msgs). Long messages split into overlapping sub-chunks (200 char overlap). Consecutive chunks overlap by 2 messages.
- **Embeddings**: `gemini-embedding-001` (3072-dim vectors), stored as float32 blobs in SQLite.
- **Search**: cosine similarity, threshold 0.25, top-K results, paginated heap merge.
- **AccessContext scoped**: RAG queries include `WHERE chat_id IN (...)` filter.
- **Backfill**: `rag_backfill` tool triggers full rebuild (swap-table pattern for atomicity).
