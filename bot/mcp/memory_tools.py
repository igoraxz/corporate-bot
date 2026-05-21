# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Memory MCP tools — unified search, facts, media, RAG, conversation context.

Named memory_tools.py to avoid clash with bot/storage/memory.py.
Extracted from bot/mcp_tools.py (Phase 1B refactor). Pure extraction — no behavior changes.
"""

import asyncio
import logging
from typing import Any

from claude_agent_sdk import tool

from bot.mcp import _safe_int, _resolve_access_ctx, _run_with_timeout

log = logging.getLogger(__name__)


# ============================================================
# MEMORY TOOLS
# ============================================================

@tool("get_recent_conversation", "Load recent conversation messages from the current chat", {
    "limit": int,
})
async def get_recent_conversation(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import get_recent_messages, format_recent_context
        from bot.hooks import get_origin_chat_id
        # Save session_key before _resolve_access_ctx pops it
        session_key = args.get("_session_key", "")
        access_ctx = _resolve_access_ctx(args)
        limit = min(_safe_int(args.get("limit"), 15), 50)
        # Default to current chat only — prevents cross-chat context bleed
        origin_cid = get_origin_chat_id(session_key) if session_key else ""
        messages = await get_recent_messages(
            limit=limit, access_ctx=access_ctx, chat_id=origin_cid,
        )
        if not messages:
            return "No recent conversation history."
        return format_recent_context(messages, origin_chat_id=origin_cid)
    return await _run_with_timeout(_do(), "get_recent_conversation")


@tool("memory_search", "UNIFIED search across ALL memory — runs keyword (FTS5), semantic (RAG), facts, and media search in PARALLEL with one call. Use this as your primary search tool. Returns merged results: facts, exact keyword matches, semantic conversation matches, and media matches (photos/docs with AI-generated descriptions). For browsing ALL facts use search_facts with action='list'.", {
    "query": str, "limit": int,
})
async def memory_search(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import search_messages, search_message_facts, search_media_cache
        from bot.storage.rag import rag_search
        access_ctx = _resolve_access_ctx(args)
        from bot.storage.db import open_db

        query = args.get("query", "")
        if not query:
            return "Please provide a search query."
        limit = min(_safe_int(args.get("limit"), 10), 20)

        # Run ALL four backends in parallel (with access filtering)
        fts_results, fact_results, rag_results, media_results = await asyncio.gather(
            search_messages(query, limit=limit, access_ctx=access_ctx),
            search_message_facts(query, limit=min(limit, 10), access_ctx=access_ctx),
            rag_search(query, top_k=min(limit, 5), access_ctx=access_ctx),
            search_media_cache(query, limit=min(limit, 5), access_ctx=access_ctx),
            return_exceptions=True,
        )

        output_parts = []

        # 1. FACTS (highest priority - structured data)
        facts = fact_results if not isinstance(fact_results, Exception) else []
        if facts:
            output_parts.append(f"FACTS ({len(facts)} matches):")
            for f in facts:
                doc = f" [doc:{f.get('doc_pointer')}]" if f.get("doc_pointer") else ""
                subj = f"[{f.get('subject')}] " if f.get("subject") else ""
                output_parts.append(f"  {subj}[{f.get('category', '')}] {f.get('fact_key', '')}: {f.get('fact_value', '')}{doc}")

        # 2. FTS5 KEYWORD MATCHES (messages)
        fts_messages = fts_results if not isinstance(fts_results, Exception) else []
        if fts_messages:
            output_parts.append(f"\nEXACT MATCHES ({len(fts_messages)} messages):")
            for m in fts_messages:
                mid = m.get("id", "?")
                ts = m.get("timestamp", "")[:16]
                src = m.get("source", "")
                user = m.get("user_name", "")
                text = m.get("text", "")
                output_parts.append(f"  [msg#{mid} {ts} {src}] {user}: {text}")

        # 3. SEMANTIC MATCHES (RAG chunks - meaning-based, pre-filtered by access_ctx in rag_search)
        rag = rag_results if not isinstance(rag_results, Exception) else []
        if rag:
            # Fetch linked facts for RAG chunks (with access filtering)
            from bot.storage.memory import _access_sql_filter as _asf
            fact_access_clause, fact_access_params = _asf(access_ctx, "source_chat_id")
            linked_facts = {}
            try:
                async with open_db() as db:
                    for r in rag:
                        cursor = await db.execute(
                            f"SELECT fact_key, fact_value, doc_pointer FROM message_facts "
                            f"WHERE source_msg_id BETWEEN ? AND ? AND expired = 0 {fact_access_clause} LIMIT 5",
                            [r["start_msg_id"], r["end_msg_id"]] + fact_access_params,
                        )
                        rows = await cursor.fetchall()
                        if rows:
                            linked_facts[r["chunk_id"]] = [dict(row) for row in rows]
            except Exception:
                pass

            output_parts.append(f"\nSEMANTIC MATCHES ({len(rag)} conversation chunks):")
            for i, r in enumerate(rag, 1):
                output_parts.append(f"\n  --- Chunk {i} (similarity={r['similarity']}, msgs {r['start_msg_id']}-{r['end_msg_id']}, {r['start_ts'][:10]}) ---")
                output_parts.append(f"  {r['chunk_text']}")
                output_parts.append(f"  [Senders: {r['senders']} | Source: {r['source']}]")
                chunk_facts = linked_facts.get(r.get("chunk_id"))
                if chunk_facts:
                    fact_strs = [f"{f['fact_key']}={f['fact_value']}" + (f" [doc:{f['doc_pointer']}]" if f.get("doc_pointer") else "") for f in chunk_facts]
                    output_parts.append(f"  [Linked facts: {'; '.join(fact_strs)}]")
                output_parts.append(f"  [Drill-down: get_messages_by_range start_id={r['start_msg_id']} end_id={r['end_msg_id']}]")

        # 4. MEDIA MATCHES (photos/docs with AI descriptions)
        media = media_results if not isinstance(media_results, Exception) else []
        if media:
            output_parts.append(f"\nMEDIA MATCHES ({len(media)} files):")
            for m in media:
                mid = m.get("id", "?")
                mtype = m.get("media_type", "")
                fname = m.get("original_filename") or m.get("filename", "")
                sender = m.get("sender_name", "")
                cached = m.get("cached_at", "")[:16]
                desc = m.get("description", "")
                desc_preview = (desc[:150] + "...") if len(desc) > 150 else desc
                fpath = m.get("file_path", "")
                output_parts.append(f"  [media#{mid} {mtype} {cached}] {fname} from {sender}")
                if desc_preview:
                    output_parts.append(f"    Description: {desc_preview}")
                if fpath:
                    output_parts.append(f"    [File: {fpath}]")

        if not output_parts:
            return "No results found across any search backend."

        # Add error notes if any backend failed
        errors = []
        if isinstance(fts_results, Exception):
            errors.append(f"FTS5 search error: {fts_results}")
        if isinstance(fact_results, Exception):
            errors.append(f"Facts search error: {fact_results}")
        if isinstance(rag_results, Exception):
            errors.append(f"RAG search error: {rag_results}")
        if isinstance(media_results, Exception):
            errors.append(f"Media search error: {media_results}")
        if errors:
            output_parts.append("\n\u26a0\ufe0f PARTIAL RESULTS - some backends failed:")
            output_parts.extend(f"  {e}" for e in errors)

        return "\n".join(output_parts)
    return await _run_with_timeout(_do(), "memory_search", timeout=30)



@tool("search_facts", "Browse or filter structured facts (knowledge graph triples). Use action='list' to see all facts (optionally filter by category or subject), or action='search' with a keyword query. For general lookups, prefer memory_search which searches facts + messages + semantics in one call. Categories are free-form (common: documents, training, operations, health, finance, bot, organization, activities). Subject = entity the fact is about (admin, user1, user2, etc.).", {
    "action": str, "query": str, "category": str, "subject": str, "limit": int,
})
async def search_facts_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import search_message_facts, get_all_message_facts
        access_ctx = _resolve_access_ctx(args)
        action = args.get("action", "search")
        category = args.get("category", "")
        subject = args.get("subject", "")
        limit = min(_safe_int(args.get("limit"), 20), 50)

        if action == "list":
            facts = await get_all_message_facts(category=category, subject=subject,
                                                access_ctx=access_ctx)
            if not facts:
                filters = []
                if category:
                    filters.append(f"category={category}")
                if subject:
                    filters.append(f"subject={subject}")
                filter_str = f" (filters: {', '.join(filters)})" if filters else ""
                return f"No facts stored yet.{filter_str}"
            lines = []
            current_subj = None
            for f in facts:
                subj = f.get("subject", "") or ""
                if subj != current_subj:
                    current_subj = subj
                    lines.append(f"\n[{subj or 'general'}]")
                doc = f" [doc: {f['doc_pointer']}]" if f.get("doc_pointer") else ""
                cat = f.get("category", "")
                cat_label = f" ({cat})" if cat else ""
                lines.append(f"  {f['fact_key']}: {f['fact_value']}{cat_label}{doc}")
            return f"Total: {len(facts)} facts\n" + "\n".join(lines)
        else:
            query = args.get("query", "")
            if not query:
                return "ERROR: 'query' is required for action='search'. Use action='list' to see all facts."
            results = await search_message_facts(query, category=category, limit=limit,
                                                    access_ctx=access_ctx)
            if not results:
                return f"No facts matching '{query}'." + (f" (category: {category})" if category else "") + " Try action='list' to see all facts."
            lines = []
            for f in results:
                src = ""
                if f.get("source_msg_id"):
                    src = f" (from msg#{f['source_msg_id']})"
                doc = f" [doc: {f['doc_pointer']}]" if f.get("doc_pointer") else ""
                subj = f"[{f.get('subject')}] " if f.get("subject") else ""
                lines.append(f"  {subj}[{f.get('category', '')}] {f['fact_key']}: {f['fact_value']}{src}{doc}")
            return f"Found {len(results)} facts:\n" + "\n".join(lines)
    return await _run_with_timeout(_do(), "search_facts")


@tool("manage_fact", "Manage stored facts directly. Actions: 'expire' (soft-delete one fact by key), 'bulk_expire' (soft-delete multiple facts by key list — max 200 per call). Use for cleanup of stale/duplicate/outdated facts. Expired facts are kept in history for audit trail.", {
    "action": str, "key": str, "keys": {"type": "array", "items": {"type": "string"}},
})
async def manage_fact_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import expire_message_fact, bulk_expire_message_facts
        access_ctx = _resolve_access_ctx(args)
        action = args.get("action", "").strip()

        if action == "expire":
            key = args.get("key", "").strip()
            if not key:
                return "ERROR: 'key' is required for action='expire'."
            ok = await expire_message_fact(key, access_ctx=access_ctx)
            return f"\u2705 Fact '{key}' expired." if ok else f"\u26a0\ufe0f Fact '{key}' not found (already expired or doesn't exist)."

        elif action == "bulk_expire":
            keys = args.get("keys", [])
            if not keys:
                return "ERROR: 'keys' list is required for action='bulk_expire'."
            if isinstance(keys, str):
                # Handle JSON array string: ["key1", "key2"]
                stripped = keys.strip()
                if stripped.startswith("["):
                    import json as _json
                    try:
                        keys = _json.loads(stripped)
                    except Exception:
                        keys = [k.strip().strip('"\'') for k in stripped.strip("[]").split(",") if k.strip()]
                else:
                    keys = [k.strip() for k in keys.split(",") if k.strip()]
            if not isinstance(keys, list):
                return "ERROR: 'keys' must be a list or comma-separated string of fact keys."
            if len(keys) > 200:
                return f"ERROR: max 200 keys per call, got {len(keys)}. Split into batches."
            invalid = [k for k in keys if not isinstance(k, str) or not k.strip()]
            if invalid:
                return f"ERROR: 'keys' contains invalid elements (must be non-empty strings): {invalid[:5]}"
            keys = [k.strip() for k in keys]
            result = await bulk_expire_message_facts(keys, access_ctx=access_ctx)
            expired = result["expired"]
            not_found = result["not_found"]
            lines = [f"\u2705 Expired {len(expired)} facts."]
            if not_found:
                lines.append(f"\u26a0\ufe0f {len(not_found)} not found: {', '.join(not_found[:10])}")
                if len(not_found) > 10:
                    lines.append(f"  ...and {len(not_found) - 10} more")
            return "\n".join(lines)

        else:
            return "ERROR: Invalid action. Use 'expire' or 'bulk_expire'."
    return await _run_with_timeout(_do(), "manage_fact")


@tool("get_document", "Retrieve a document linked to a fact. Input: fact_key (e.g. 'dad_passport'). Returns the file path if a document is linked, or suggests how to find it. Use this when someone asks 'send me my passport' or similar.", {
    "fact_key": str,
})
async def get_document_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import get_fact_document, search_message_facts
        access_ctx = _resolve_access_ctx(args)
        import os
        fact_key = args.get("fact_key", "").strip()
        if not fact_key:
            return "ERROR: 'fact_key' is required. Use search_facts to find the right key first."

        # Direct lookup by key (with access filtering)
        result = await get_fact_document(fact_key, access_ctx=access_ctx)
        if result and result.get("doc_pointer"):
            path = result["doc_pointer"]
            if os.path.exists(path):
                return f"Document found: {path}\nFact: {result['fact_key']} = {result['fact_value']}\nUse telegram_send_document or send_file to deliver this to the user."
            else:
                return f"Document path recorded but file not found at: {path}\nFact: {result['fact_key']} = {result['fact_value']}\nThe file may have been moved or deleted."
        elif result:
            return f"Fact found but no document linked:\n  {result['fact_key']}: {result['fact_value']}\n  Category: {result.get('category', 'general')}\nNo doc_pointer set. If the user has sent this document before, try search_media to find it."

        # Fallback: FTS search (with access filtering)
        results = await search_message_facts(fact_key, limit=5, access_ctx=access_ctx)
        if results:
            lines = ["No exact key match. Similar facts found:"]
            for f in results:
                doc = f" [doc: {f['doc_pointer']}]" if f.get("doc_pointer") else ""
                lines.append(f"  {f['fact_key']}: {f['fact_value']}{doc}")
            return "\n".join(lines)

        return f"No fact found for key '{fact_key}'. Use search_facts to browse available facts."
    return await _run_with_timeout(_do(), "get_document")


@tool("get_message_context", "Get full conversation context around a specific message ID. Use after memory_search finds a specific message and you need surrounding context.", {
    "message_id": int, "before": int, "after": int,
})
async def get_message_context(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import get_messages_around
        access_ctx = _resolve_access_ctx(args)
        messages = await get_messages_around(
            _safe_int(args.get("message_id"), 0),
            before=_safe_int(args.get("before"), 10),
            after=_safe_int(args.get("after"), 10),
            access_ctx=access_ctx,
        )
        if not messages:
            return "No messages found around this ID."
        lines = []
        for m in messages:
            mid = m.get("id", "?")
            ts = m.get("timestamp", "")[:16]
            src = m.get("source", "")
            user = m.get("user_name", "")
            role = m.get("role", "")
            text = m.get("text", "")
            marker = " <<<" if mid == _safe_int(args.get("message_id"), 0) else ""
            lines.append(f"[msg#{mid} {ts} {src} {role}] {user}: {text}{marker}")
        return "\n".join(lines)
    return await _run_with_timeout(_do(), "get_message_context")


@tool("get_messages_by_range", "Load FULL conversation messages + linked facts. 'Zoom in' after memory_search returns msg ID ranges. Three query modes (use exactly one): 1) start_id+end_id — contiguous ID range, 2) msg_ids — list of specific IDs, 3) from_date+to_date — ISO date/datetime range (e.g. '2026-03-01'). Returns complete message text, sender, timestamp, platform, and all linked facts.", {
    "start_id": int, "end_id": int, "msg_ids": list, "from_date": str, "to_date": str,
})
async def get_messages_by_range(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import get_messages_by_range as _get_range
        access_ctx = _resolve_access_ctx(args)
        start_id = _safe_int(args.get("start_id"), 0) if args.get("start_id") is not None else None
        end_id = _safe_int(args.get("end_id"), 0) if args.get("end_id") is not None else None
        msg_ids_raw = args.get("msg_ids")
        msg_ids = [_safe_int(x, 0) for x in msg_ids_raw] if msg_ids_raw else None
        from_date = args.get("from_date")
        to_date = args.get("to_date")

        # Validate: exactly one query mode
        has_range = start_id is not None and end_id is not None
        has_ids = bool(msg_ids)
        has_time = bool(from_date) and bool(to_date)
        modes_used = sum([has_range, has_ids, has_time])
        if modes_used == 0:
            return "ERROR: Provide one of: start_id+end_id, msg_ids, or from_date+to_date."
        if modes_used > 1:
            return "ERROR: Use only one query mode at a time."

        # Safety limits
        if has_range and end_id - start_id > 100:
            return "ERROR: Range too large (max 100 messages). Narrow down first."
        if has_ids and len(msg_ids) > 100:
            return "ERROR: Too many IDs (max 100). Narrow down first."

        result = await _get_range(
            start_id=start_id, end_id=end_id,
            msg_ids=msg_ids,
            from_date=from_date, to_date=to_date,
            access_ctx=access_ctx,
        )
        messages = result["messages"]
        facts = result["facts"]

        if not messages:
            return "No messages found for the given query."

        # Build facts index by source_msg_id for inline display
        facts_by_msg: dict[int, list] = {}
        for f in facts:
            mid = f.get("source_msg_id")
            if mid:
                facts_by_msg.setdefault(mid, []).append(f)

        # Header
        if has_range:
            header = f"MESSAGES {start_id}-{end_id}"
        elif has_ids:
            header = f"MESSAGES (IDs: {','.join(str(i) for i in msg_ids[:10])}{'...' if len(msg_ids) > 10 else ''})"
        else:
            header = f"MESSAGES {from_date} to {to_date}"
        parts = [f"{header} ({len(messages)} messages, {len(facts)} linked facts):"]

        for m in messages:
            mid = m["id"]
            ts = m.get("timestamp", "")[:16]
            src = m.get("source", "")
            user = m.get("user_name", "")
            role = m.get("role", "")
            text = m.get("text", "")
            parts.append(f"\n[msg#{mid} {ts} {src} {role}] {user}: {text}")
            # Show linked facts inline
            msg_facts = facts_by_msg.get(mid)
            if msg_facts:
                for f in msg_facts:
                    expired = " [SUPERSEDED]" if f.get("expired") or f.get("superseded_by") else ""
                    doc = f" [doc:{f['doc_pointer']}]" if f.get("doc_pointer") else ""
                    parts.append(f"  -> FACT [{f.get('category', '')}] {f['fact_key']}: {f['fact_value']}{doc}{expired}")

        return "\n".join(parts)
    return await _run_with_timeout(_do(), "get_messages_by_range")


@tool("search_media", "Search cached media files (photos, documents, voice messages, etc.) by keyword", {
    "query": str, "media_type": str, "limit": int,
})
async def search_media(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.memory import search_media_cache, list_cached_media, ensure_media_file
        access_ctx = _resolve_access_ctx(args)
        import os
        query = args.get("query", "")
        if query:
            results = await search_media_cache(query, limit=_safe_int(args.get("limit"), 10),
                                               access_ctx=access_ctx)
        else:
            results = await list_cached_media(media_type=args.get("media_type", ""),
                                              limit=_safe_int(args.get("limit"), 10),
                                              access_ctx=access_ctx)
        if not results:
            return "No cached media files found."
        lines = ["Cached media files:"]
        for r in results:
            size_kb = r.get("file_size", 0) / 1024
            # Check file availability and attempt re-fetch if needed
            file_path = r.get("file_path", "")
            if file_path and os.path.exists(file_path):
                status_icon = "\u2705"  # available
            else:
                media_status = await ensure_media_file(r)
                if media_status["status"] == "refetched":
                    status_icon = "\U0001f504"  # re-fetched from TG
                    file_path = media_status["file_path"]
                else:
                    status_icon = "\u274c"  # unavailable
                    file_path = f"[UNAVAILABLE: {media_status.get('reason', 'file missing')}]"
            lines.append(
                f"  {status_icon} [{r.get('cached_at', '')[:10]}] {r.get('media_type', '')} | "
                f"{r.get('original_filename', r.get('filename', ''))} | "
                f"{size_kb:.0f}KB | from {r.get('sender_name', '?')} | "
                f"path: {file_path}"
            )
            if r.get("description"):
                lines.append(f"    {r['description'][:200]}")
        return "\n".join(lines)
    return await _run_with_timeout(_do(), "search_media")


# ============================================================
# RAG ADMIN TOOLS
# ============================================================

@tool("rag_backfill", "Rebuild ALL RAG chunks from scratch. Reads all conversation messages, creates size-aware chunks (char-budget, long-msg splitting), embeds them. Run this to (re)build the semantic search index.", {})
async def rag_backfill_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.rag import backfill_chunks, get_rag_stats

        result = await backfill_chunks()

        if "error" in result:
            return f"Backfill error: {result['error']}"

        stats = await get_rag_stats()
        return (
            f"RAG backfill complete!\n"
            f"Messages processed: {result['total_messages']}\n"
            f"Chunks created: {result['chunks_created']}\n"
            f"Chunks failed: {result.get('chunks_failed', 0)}\n"
            f"Time: {result['time_sec']}s\n"
            f"Index: {stats['total_chunks']} chunks covering msgs {stats['msg_range']}"
        )
    return await _run_with_timeout(_do(), "rag_backfill", timeout=300)


@tool("rag_stats", "Get RAG chunk index statistics — number of chunks, message coverage range.", {})
async def rag_stats_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.storage.rag import get_rag_stats
        stats = await get_rag_stats()
        return (
            f"RAG Stats:\n"
            f"Total messages: {stats['total_messages']}\n"
            f"Total chunks: {stats['total_chunks']}\n"
            f"Message range: {stats['msg_range']}"
        )
    return await _run_with_timeout(_do(), "rag_stats")


# ============================================================
# Tool list for server registration
# ============================================================

MEMORY_TOOLS = [
    memory_search, get_recent_conversation, search_facts_tool, get_document_tool,
    get_message_context, get_messages_by_range, search_media,
    rag_backfill_tool, rag_stats_tool, manage_fact_tool,
]

__all__ = [
    "memory_search", "get_recent_conversation", "search_facts_tool",
    "get_document_tool", "manage_fact_tool", "get_message_context",
    "get_messages_by_range", "search_media", "rag_backfill_tool", "rag_stats_tool",
    "MEMORY_TOOLS",
]
