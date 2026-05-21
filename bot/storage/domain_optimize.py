# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Domain self-optimization — analyze chat histories and recommend knowledge improvements (CB-102/103).

6-Layer OPTIMIZATION layer: scheduled analysis of chat histories per domain
via Gemini Flash. Detects gaps, recommends upgrades to INSTRUCTIONS, FACTS,
and KNOWLEDGE layers. Domain owner approves/rejects via MCP tool.

Usage:
    # Scheduled task (daily/weekly):
    result = await analyze_domain_chats("teamwork")

    # Owner reviews:
    recs = await list_recommendations("teamwork", status="pending")
    await approve_recommendation(rec_id, actor="telegram:123456789")
"""
import json
import logging
import time

log = logging.getLogger(__name__)


# ============================================================
# Database schema
# ============================================================

async def init_optimization_tables():
    """Create domain_recommendations table if not exists."""
    from bot.storage.db import open_db

    async with open_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS domain_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain_id TEXT NOT NULL,
                target_layer TEXT NOT NULL,
                rec_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                reviewed_by TEXT DEFAULT '',
                review_notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT DEFAULT '',
                applied_at TEXT DEFAULT ''
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_domain_recs_domain "
            "ON domain_recommendations(domain_id, status)"
        )
        await db.commit()


# ============================================================
# Analysis engine
# ============================================================

async def analyze_domain_chats(domain_id: str, days: int = 7) -> dict:
    """Analyze recent chat histories for a domain and generate recommendations.

    Uses Gemini Flash for cheap, fast analysis of conversation patterns.
    Returns: {"recommendations": N, "analyzed_messages": N}
    """
    from bot.storage.db import open_db

    # Fetch recent messages from chats that have this domain as write_domain
    # (these are the chats contributing to the domain's knowledge)
    async with open_db() as db:
        # Get chat_ids that write to this domain (scoped — not all chats)
        import datetime
        since = (datetime.datetime.now(datetime.timezone.utc) -
                 datetime.timedelta(days=days)).isoformat()
        # Only analyze messages from chats whose write_domain matches
        _domain_chat_ids: list[str] = []
        try:
            from bot.chat_registry import all_entries
            for _ck, _ce in all_entries().items():
                if _ce.get("write_domain") == domain_id:
                    parts = _ck.split(":")
                    if len(parts) >= 2:
                        _domain_chat_ids.append(parts[1])
        except Exception:
            pass
        if not _domain_chat_ids:
            return {"recommendations": 0, "analyzed_messages": 0, "note": "no chats with this write_domain"}
        _chat_placeholders = ",".join("?" for _ in _domain_chat_ids)
        cursor = await db.execute(
            f"SELECT text, role, chat_id FROM messages "
            f"WHERE timestamp > ? AND role IN ('user', 'assistant') "
            f"AND chat_id IN ({_chat_placeholders}) "
            f"ORDER BY timestamp DESC LIMIT 500",
            [since] + _domain_chat_ids,
        )
        messages = await cursor.fetchall()

    if not messages:
        return {"recommendations": 0, "analyzed_messages": 0}

    # Build analysis prompt
    _chat_sample = "\n".join(
        f"[{m[1]}] {m[0][:200]}" for m in messages[:100]
    )

    analysis_prompt = f"""Analyze these recent chat messages for the "{domain_id}" knowledge domain.

Identify:
1. Questions that went unanswered or required web search (knowledge gaps)
2. Facts that were corrected by users (stale/wrong knowledge)
3. Frequently repeated questions (FAQ candidates → should be in INSTRUCTIONS)
4. Patterns that suggest missing skills or prompt guidance
5. Knowledge that should be documented but isn't

For each finding, provide:
- target_layer: INSTRUCTIONS, FACTS, or KNOWLEDGE
- rec_type: gap, correction, faq, skill_needed, doc_needed
- title: one-line summary
- content: detailed recommendation (what to add/change)
- evidence: brief excerpt from chat showing the issue

Return as JSON array. Max 5 recommendations per analysis.

Recent messages:
{_chat_sample[:5000]}"""

    # Call Gemini Flash for analysis (use google.genai directly — no generate_text wrapper)
    try:
        from google import genai
        import config as _cfg
        _gemini_key = getattr(_cfg, "GEMINI_API_KEY", "")
        if not _gemini_key:
            return {"recommendations": 0, "analyzed_messages": len(messages), "error": "No GEMINI_API_KEY"}
        client = genai.Client(api_key=_gemini_key)
        _resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=analysis_prompt,
        )
        response = _resp.text if _resp and _resp.text else ""
        # Parse recommendations from response
        recs = _parse_recommendations(response, domain_id)
    except Exception as e:
        log.warning("Domain optimization analysis failed for %s: %s", domain_id, e)
        return {"recommendations": 0, "analyzed_messages": len(messages), "error": str(e)}

    # Store recommendations
    stored = 0
    for rec in recs:
        try:
            await _store_recommendation(domain_id, rec)
            stored += 1
        except Exception as e:
            log.warning("Failed to store recommendation: %s", e)

    log.info("Domain optimization: %s — %d messages analyzed, %d recommendations",
             domain_id, len(messages), stored)
    return {"recommendations": stored, "analyzed_messages": len(messages)}


def _parse_recommendations(response: str, domain_id: str) -> list[dict]:
    """Parse Gemini Flash response into recommendation dicts."""
    # Try to extract JSON array from response
    try:
        # Find JSON array in response
        start = response.find("[")
        end = response.rfind("]") + 1
        if start >= 0 and end > start:
            recs = json.loads(response[start:end])
            if isinstance(recs, list):
                return [
                    {
                        "target_layer": r.get("target_layer", "FACTS"),
                        "rec_type": r.get("rec_type", "gap"),
                        "title": r.get("title", "")[:200],
                        "content": r.get("content", "")[:1000],
                        "evidence": r.get("evidence", "")[:500],
                    }
                    for r in recs[:5]  # Max 5 per analysis
                    if r.get("title")
                ]
    except json.JSONDecodeError:
        pass
    return []


async def _store_recommendation(domain_id: str, rec: dict):
    """Store a single recommendation in the database."""
    from bot.storage.db import open_db
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with open_db() as db:
        await db.execute(
            "INSERT INTO domain_recommendations "
            "(domain_id, target_layer, rec_type, title, content, evidence, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (domain_id, rec["target_layer"], rec["rec_type"],
             rec["title"], rec["content"], rec.get("evidence", ""), now),
        )
        await db.commit()


# ============================================================
# Recommendation management (CB-103)
# ============================================================

async def list_recommendations(domain_id: str, status: str = "pending",
                                limit: int = 20) -> list[dict]:
    """List recommendations for a domain, filtered by status."""
    from bot.storage.db import open_db

    async with open_db() as db:
        if status:
            cursor = await db.execute(
                "SELECT id, domain_id, target_layer, rec_type, title, content, "
                "evidence, status, reviewed_by, created_at, reviewed_at "
                "FROM domain_recommendations WHERE domain_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (domain_id, status, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT id, domain_id, target_layer, rec_type, title, content, "
                "evidence, status, reviewed_by, created_at, reviewed_at "
                "FROM domain_recommendations WHERE domain_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (domain_id, limit),
            )
        rows = await cursor.fetchall()

    return [
        {
            "id": r[0], "domain_id": r[1], "target_layer": r[2],
            "rec_type": r[3], "title": r[4], "content": r[5],
            "evidence": r[6], "status": r[7], "reviewed_by": r[8],
            "created_at": r[9], "reviewed_at": r[10],
        }
        for r in rows
    ]


async def review_recommendation(rec_id: int, action: str, actor: str = "",
                                 notes: str = "") -> dict:
    """Approve or reject a recommendation.

    action: approve, reject, modify
    On approve: auto-applies the change to the target layer.
    """
    # Fail-closed: require actor identity for authorization
    if not actor:
        return {"error": "Authorization failed: actor identity required."}

    from bot.storage.db import open_db
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async with open_db() as db:
        cursor = await db.execute(
            "SELECT * FROM domain_recommendations WHERE id = ?", (rec_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"error": f"Recommendation {rec_id} not found"}

        # CB-103: Authorization check — only domain owner or bot_admin
        _rec_domain = row[1]
        if actor:
            try:
                from bot.rbac import get_engine
                from bot.rbac.models import Subject
                engine = get_engine()
                _auth = await engine.check_access(
                    Subject(user_id=actor), "admin", "knowledge_domain", _rec_domain,
                )
                if not _auth.allowed:
                    return {"error": "Permission denied: only domain owner/admin can review recommendations."}
            except Exception as _auth_err:
                log.warning("RBAC check failed for review_recommendation: %s", _auth_err)
                return {"error": "Authorization check failed. Please try again."}

        new_status = "approved" if action == "approve" else "rejected"
        await db.execute(
            "UPDATE domain_recommendations SET status = ?, reviewed_by = ?, "
            "review_notes = ?, reviewed_at = ? WHERE id = ?",
            (new_status, actor, notes, now, rec_id),
        )
        await db.commit()

    rec = {
        "id": row[0], "domain_id": row[1], "target_layer": row[2],
        "rec_type": row[3], "title": row[4], "content": row[5],
    }

    # Auto-apply on approve
    if action == "approve":
        try:
            await _apply_recommendation(rec)
            async with open_db() as db:
                await db.execute(
                    "UPDATE domain_recommendations SET applied_at = ? WHERE id = ?",
                    (now, rec_id),
                )
                await db.commit()
            rec["applied"] = True
        except Exception as e:
            log.warning("Failed to auto-apply recommendation %d: %s", rec_id, e)
            rec["applied"] = False
            rec["apply_error"] = str(e)

    return {"success": True, "action": action, "recommendation": rec}


async def _apply_recommendation(rec: dict):
    """Auto-apply an approved recommendation to the target layer."""
    layer = rec.get("target_layer", "")
    domain_id = rec.get("domain_id", "")
    content = rec.get("content", "")

    if layer == "FACTS":
        # Save as a domain fact
        from bot.storage.facts import save_message_fact
        key = f"rec_{rec['id']}_{rec.get('rec_type', 'gap')}"
        await save_message_fact(
            key=key,
            value=content[:500],
            category="recommendations",
            domain_id=domain_id,
            source_chat_id="system:optimization",
        )
    elif layer == "INSTRUCTIONS":
        # Append to domain custom_instructions
        from bot.storage.domains import get_domain, register_domain, DomainConfig
        import yaml
        d = await get_domain(domain_id)
        if d and d.get("config_yaml"):
            dc = DomainConfig.from_yaml(yaml.safe_load(d["config_yaml"]) or {})
            existing = dc.knowledge.custom_instructions or ""
            dc.knowledge.custom_instructions = f"{existing}\n\n{content}".strip()
            dc.id = domain_id
            await register_domain(dc)
    elif layer == "KNOWLEDGE":
        # Flag for manual indexing (can't auto-index without content source)
        log.info("Recommendation %d approved for KNOWLEDGE layer — "
                 "requires manual document creation/indexing", rec["id"])

    log.info("Applied recommendation %d to %s layer of domain %s",
             rec["id"], layer, domain_id)
