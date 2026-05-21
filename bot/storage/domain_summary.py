# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Domain hot-context summary generator.

Generates compact summaries (~2-5KB) of domain knowledge for injection
into the system prompt. This gives the model immediate awareness of domain
content without requiring search tool calls.

Summary content (in priority order):
1. LLM-generated knowledge map (Gemini Flash — compressed topic summary with
   dive-deeper pointers), when available
2. Fallback: file-list summary:
   a. Domain description (from registry)
   b. Document inventory (file paths from domain_documents table)
   c. Key facts tagged with this domain (from message_facts)
"""

import logging
import time

from bot.storage.db import open_db

log = logging.getLogger(__name__)


async def generate_domain_summary(domain_id: str, budget: int = 3000) -> str:
    """Generate a hot-context summary for a domain.

    Tries LLM-generated knowledge map first (higher quality), falls back
    to file-list summary if Gemini is unavailable or fails.

    Args:
        domain_id: Domain to summarize
        budget: Max characters for the summary

    Returns: Formatted summary string for prompt injection
    """
    async with open_db() as db:
        # Get domain info
        cursor = await db.execute(
            "SELECT name, description FROM domain_registry WHERE domain_id = ?",
            (domain_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return ""

        name = row["name"] or domain_id
        description = row["description"] or ""

        # Get document inventory
        cursor = await db.execute(
            "SELECT file_path, chunk_count FROM domain_documents "
            "WHERE domain_id = ? AND status = 'active' ORDER BY file_path",
            (domain_id,)
        )
        docs = await cursor.fetchall()

        # Get domain-scoped facts
        cursor = await db.execute(
            "SELECT fact_key, fact_value, category FROM message_facts "
            "WHERE domain_id = ? AND expired = 0 ORDER BY category, fact_key LIMIT 20",
            (domain_id,)
        )
        facts = await cursor.fetchall()

    # Try LLM-generated knowledge map first (higher quality)
    llm_summary = await _generate_llm_summary(
        domain_id, name, description,
        [dict(d) for d in docs], [dict(f) for f in facts],
        budget=budget,
    )
    if llm_summary:
        return llm_summary

    # Fallback: file-list summary (original behavior)
    return _build_file_list_summary(description, docs, facts, budget)


def _build_file_list_summary(
    description: str,
    docs: list,
    facts: list,
    budget: int,
) -> str:
    """Build a basic file-list summary (fallback when Gemini unavailable)."""
    lines: list[str] = []
    used = 0

    # 1. Description (always included)
    if description:
        desc_text = description[:200] + "..." if len(description) > 200 else description
        lines.append(f"  {desc_text}")
        used += len(lines[-1])

    # 2. Document inventory
    if docs and used < budget - 200:
        doc_list = [f"    \u2022 {d['file_path']}" for d in docs[:15]]
        doc_section = f"  Documents ({len(docs)} indexed):\n" + "\n".join(doc_list)
        if used + len(doc_section) < budget:
            lines.append(doc_section)
            used += len(doc_section)
        else:
            # Just show count
            lines.append(f"  Documents: {len(docs)} indexed files")
            used += 30

    # 3. Key facts
    if facts and used < budget - 100:
        facts_lines: list[str] = []
        for f in facts[:10]:
            val_display = (f["fact_value"] or "")[:80]
            facts_lines.append(f"    {f['fact_key']}: {val_display}")
        facts_section = "  Key facts:\n" + "\n".join(facts_lines)
        if used + len(facts_section) < budget:
            lines.append(facts_section)

    return "\n".join(lines)


async def _get_chunk_previews(domain_id: str, limit: int = 20) -> list[dict]:
    """Get text previews of chunks for a domain (for LLM summarization input)."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT source_file, chunk_text FROM rag_chunks "
            "WHERE domain_id = ? ORDER BY source_file, rowid LIMIT ?",
            (domain_id, limit),
        )
        return [{"source_file": r["source_file"], "text": r["chunk_text"]} for r in await cursor.fetchall()]


async def _generate_llm_summary(
    domain_id: str,
    domain_name: str,
    domain_description: str,
    doc_inventory: list[dict],
    domain_facts: list[dict],
    budget: int = 3000,
) -> str:
    """Use Gemini Flash to generate a compressed knowledge map.

    Input: domain metadata + document inventory + key facts + chunk previews
    Output: structured topic summary with dive-deeper pointers

    Returns empty string if Gemini is unavailable or fails (caller falls back).
    """
    import os
    from config import GEMINI_API_KEY as gemini_api_key
    if not gemini_api_key:
        return ""  # Fallback to file-list summary

    # Build input context for Gemini
    doc_list = "\n".join(
        f"  - {d['file_path']} ({d.get('chunk_count', '?')} chunks)"
        for d in doc_inventory[:30]
    )
    facts_list = "\n".join(
        f"  - {f['fact_key']}: {(f.get('fact_value') or '')[:100]}"
        for f in domain_facts[:20]
    )

    # Read first ~300 chars of each document's first chunk for topic extraction
    chunk_previews = await _get_chunk_previews(domain_id, limit=20)
    previews_text = "\n---\n".join(
        f"[{p['source_file']}]\n{p['text'][:300]}" for p in chunk_previews
    )

    prompt = (
        f'Generate a compressed knowledge map for the "{domain_name}" knowledge domain.\n'
        f"\n"
        f"Domain description: {domain_description}\n"
        f"\n"
        f"Documents indexed:\n"
        f"{doc_list}\n"
        f"\n"
        f"Key facts stored:\n"
        f"{facts_list}\n"
        f"\n"
        f"Sample content from documents:\n"
        f"{previews_text[:5000]}\n"
        f"\n"
        f"Generate a compressed summary (max {budget} characters) with this structure:\n"
        f"\n"
        f"TOPICS: [comma-separated list of main topics covered]\n"
        f"\n"
        f"KEY INFORMATION:\n"
        f'  \u2022 [most important fact or procedure, 1 line each]\n'
        f'  \u2022 [up to 10 key items]\n'
        f"\n"
        f"DIVE DEEPER:\n"
        f'  \u2022 [topic] \u2192 search_knowledge("[suggested query]")\n'
        f'  \u2022 [up to 5 search suggestions for common questions]\n'
        f"\n"
        f"Keep it compressed \u2014 this goes into the AI's system prompt as a table of contents.\n"
        f'The AI will call search_knowledge() to get full details when needed.'
    )

    try:
        from google import genai
        import asyncio

        client = genai.Client(api_key=gemini_api_key)
        model_name = os.environ.get("GEMINI_MODEL_DESCRIBE", "gemini-2.5-flash-lite")

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
        )

        if not response or not response.text:
            return ""

        # Trim to budget
        result = response.text[:budget]
        if len(response.text) > budget:
            # Trim at last newline to avoid cut-off mid-line
            last_nl = result.rfind("\n")
            if last_nl > budget * 0.7:
                result = result[:last_nl]
        return result

    except Exception as e:
        log.warning("Gemini knowledge map generation failed for %s: %s", domain_id, e)
        return ""


async def refresh_domain_summary(domain_id: str) -> str:
    """Regenerate and cache the hot summary for a domain."""
    from bot.storage.domains import get_domain

    domain = await get_domain(domain_id)
    if not domain:
        return ""

    # Get budget from config
    config_yaml = domain.get("config_yaml", "")
    budget = 3000  # default
    if config_yaml:
        try:
            import yaml
            cfg = yaml.safe_load(config_yaml)
            d = cfg.get("domain", cfg)
            knowledge = d.get("knowledge", {})
            budget = knowledge.get("hot_context_budget", 3000)
        except Exception:
            pass

    summary = await generate_domain_summary(domain_id, budget)

    # Cache in domain_registry
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        await db.execute(
            "UPDATE domain_registry SET hot_summary = ?, hot_summary_updated_at = ? "
            "WHERE domain_id = ?",
            (summary, now, domain_id)
        )
        await db.commit()

    log.info("Refreshed hot summary for domain %s (%d chars)", domain_id, len(summary))
    return summary


async def get_domain_hot_summaries(domain_ids: list[str]) -> dict[str, str]:
    """Get cached hot summaries for multiple domains.

    Returns dict of domain_id -> summary text.
    Missing/empty summaries are auto-generated.
    """
    if not domain_ids:
        return {}

    async with open_db() as db:
        placeholders = ",".join("?" * len(domain_ids))
        cursor = await db.execute(
            f"SELECT domain_id, hot_summary FROM domain_registry "
            f"WHERE domain_id IN ({placeholders}) AND status = 'active'",
            tuple(domain_ids)
        )
        rows = await cursor.fetchall()

    result: dict[str, str] = {}
    for row in rows:
        did = row["domain_id"]
        summary = row["hot_summary"] or ""
        if not summary:
            # Auto-generate if missing
            summary = await refresh_domain_summary(did)
        result[did] = summary

    return result
