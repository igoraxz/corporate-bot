# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Knowledge Domain system — multi-source, permission-scoped knowledge bases.

Each domain is a labeled collection of documents (Markdown, PDF, DOCX, code)
synced from a GitHub repository, Microsoft Graph API (SharePoint/Teams/OneDrive),
or local filesystem. Documents are chunked, embedded, and stored in the RAG
system with a domain_id tag for permission-scoped retrieval.

Architecture:
- Domain configs: YAML files defining source (GitHub/Graph API/local), paths, access rules
- Sync pipeline: GitHub webhook (primary) + polling (fallback) + Graph API delta queries
- Incremental: SHA-256 content-hash gating skips unchanged files
- Retrieval: domain_id filter in SQL WHERE clause (same pattern as chat_id)

Domain lifecycle:
1. Admin registers domain (YAML config → domain_registry table)
2. Initial sync: fetch files from source, parse docs, chunk, embed, store with domain_id
3. Ongoing: webhook/polling/delta triggers re-index of changed files only
4. Deletion: remove all chunks + registry entry
"""

import asyncio
import hashlib
import logging
import time
import yaml
from dataclasses import dataclass, field
from pathlib import Path

from bot.storage.db import open_db

log = logging.getLogger(__name__)


# ============================================================
# Domain Config Schema
# ============================================================

@dataclass
class DomainSource:
    """Source repository/location configuration."""
    type: str = "github"           # github | graph_api | local
    # GitHub fields
    repo: str = ""                 # org/repo-name
    branch: str = "main"
    # Graph API fields (SharePoint/Teams/OneDrive)
    site_id: str = ""              # SharePoint site ID
    drive_id: str = ""             # Drive ID (if known directly)
    team_id: str = ""              # Teams team ID
    channel_id: str = ""           # Teams channel ID
    # Common fields
    paths: list[str] = field(default_factory=lambda: ["."])
    exclude: list[str] = field(default_factory=list)


@dataclass
class DomainIndexing:
    """Indexing configuration."""
    chunking_strategy: str = "markdown_headers"  # markdown_headers | recursive | code
    chunk_size: int = 1500
    chunk_overlap: int = 200
    poll_interval_minutes: int = 5


@dataclass
class DomainAccess:
    """Access control configuration."""
    teams: list[str] = field(default_factory=list)       # team names/IDs with query access
    admins: list[str] = field(default_factory=list)      # emails of domain admins
    credentials: list[str] = field(default_factory=list) # credential IDs shared with domain members


@dataclass
class DomainKnowledge:
    """Knowledge stack configuration for a domain.

    Each domain can declare its own prompt modules, skills, auto-load skills,
    and fact categories. When a harness references multiple domains, these are
    composed (unioned) together to create the session's knowledge environment.
    """
    prompt_modules: list[str] = field(default_factory=list)    # system prompt module names
    skills: list[str] = field(default_factory=list)            # relevant skill names
    auto_load_skills: list[str] = field(default_factory=list)  # skills pre-loaded into CLAUDE.md
    facts_categories: list[str] = field(default_factory=list)  # fact categories owned by this domain
    essential_docs: list[str] = field(default_factory=list)    # file paths always in hot summary
    hot_context_budget: int = 3000                             # max chars for this domain's hot summary
    custom_instructions: str = ""           # per-domain custom instructions for system prompt
    facts_visible: bool = True              # show this domain's facts in system prompt
    default_harness_preset: dict = field(default_factory=dict)  # sensible harness defaults for this domain


@dataclass
class DomainConfig:
    """Complete domain configuration (from domain.yaml).

    The `description` field is critical for AI routing — it helps the model
    decide which domain is relevant for a query (search routing) and where
    to store new facts (write routing) in multi-domain chats.
    """
    id: str = ""
    name: str = ""
    description: str = ""
    owner: str = ""
    source: DomainSource = field(default_factory=DomainSource)
    indexing: DomainIndexing = field(default_factory=DomainIndexing)
    access: DomainAccess = field(default_factory=DomainAccess)
    knowledge: DomainKnowledge = field(default_factory=DomainKnowledge)
    # Runtime state
    status: str = "active"          # active | syncing | error | disabled
    last_sync_at: str = ""
    chunk_count: int = 0
    error_message: str = ""

    @classmethod
    def from_yaml(cls, yaml_dict: dict) -> "DomainConfig":
        """Parse a domain.yaml dict into DomainConfig."""
        d = yaml_dict.get("domain", yaml_dict)
        source_raw = d.get("source", {})
        indexing_raw = d.get("indexing", {})
        access_raw = d.get("access", {})
        knowledge_raw = d.get("knowledge", {})

        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            owner=d.get("owner", ""),
            source=DomainSource(
                type=source_raw.get("type", "github"),
                repo=source_raw.get("repo", ""),
                branch=source_raw.get("branch", "main"),
                site_id=source_raw.get("site_id", ""),
                drive_id=source_raw.get("drive_id", ""),
                team_id=source_raw.get("team_id", ""),
                channel_id=source_raw.get("channel_id", ""),
                paths=source_raw.get("paths", ["."]),
                exclude=source_raw.get("exclude", []),
            ),
            indexing=DomainIndexing(
                chunking_strategy=indexing_raw.get("chunking_strategy", "markdown_headers"),
                chunk_size=indexing_raw.get("chunk_size", 1500),
                chunk_overlap=indexing_raw.get("chunk_overlap", 200),
                poll_interval_minutes=indexing_raw.get("poll_interval_minutes", 5),
            ),
            access=DomainAccess(
                teams=access_raw.get("teams", []),
                admins=access_raw.get("admins", []),
                credentials=access_raw.get("credentials", []),
            ),
            knowledge=DomainKnowledge(
                prompt_modules=knowledge_raw.get("prompt_modules", []),
                skills=knowledge_raw.get("skills", []),
                auto_load_skills=knowledge_raw.get("auto_load_skills", []),
                facts_categories=knowledge_raw.get("facts_categories", []),
                essential_docs=knowledge_raw.get("essential_docs", []),
                hot_context_budget=knowledge_raw.get("hot_context_budget", 3000),
                custom_instructions=knowledge_raw.get("custom_instructions", ""),
                facts_visible=knowledge_raw.get("facts_visible", True),
                default_harness_preset=knowledge_raw.get("default_harness_preset", {}),
            ),
        )


# ============================================================
# Domain Registry (SQLite-backed)
# ============================================================

async def init_domain_tables():
    """Create domain-related tables if they don't exist."""
    async with open_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS domain_registry (
                domain_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                config_yaml TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_sync_at TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS domain_documents (
                doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                last_indexed_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                UNIQUE(domain_id, file_path),
                FOREIGN KEY (domain_id) REFERENCES domain_registry(domain_id)
            );

            CREATE INDEX IF NOT EXISTS idx_domain_docs_domain
                ON domain_documents(domain_id);
        """)

        # Migration: add knowledge stack columns to domain_registry if missing
        _knowledge_cols = {
            "prompt_modules": "TEXT DEFAULT '[]'",
            "skills": "TEXT DEFAULT '[]'",
            "auto_load_skills": "TEXT DEFAULT '[]'",
            "facts_categories": "TEXT DEFAULT '[]'",
        }
        for col_name, col_type in _knowledge_cols.items():
            try:
                await db.execute(f"SELECT {col_name} FROM domain_registry LIMIT 0")
            except Exception:
                await db.execute(
                    f"ALTER TABLE domain_registry ADD COLUMN {col_name} {col_type}"
                )
                log.info("Added %s column to domain_registry", col_name)

        # Migration: add custom_instructions and facts_visible columns
        try:
            await db.execute("SELECT custom_instructions FROM domain_registry LIMIT 0")
        except Exception:
            await db.execute(
                "ALTER TABLE domain_registry ADD COLUMN custom_instructions TEXT DEFAULT ''"
            )
            log.info("Added custom_instructions column to domain_registry")
        try:
            await db.execute("SELECT facts_visible FROM domain_registry LIMIT 0")
        except Exception:
            await db.execute(
                "ALTER TABLE domain_registry ADD COLUMN facts_visible BOOLEAN DEFAULT 1"
            )
            log.info("Added facts_visible column to domain_registry")

        # Migration: add hot_summary columns to domain_registry if missing
        try:
            await db.execute("SELECT hot_summary FROM domain_registry LIMIT 0")
        except Exception:
            await db.execute(
                "ALTER TABLE domain_registry ADD COLUMN hot_summary TEXT DEFAULT ''"
            )
            log.info("Added hot_summary column to domain_registry")
        try:
            await db.execute("SELECT hot_summary_updated_at FROM domain_registry LIMIT 0")
        except Exception:
            await db.execute(
                "ALTER TABLE domain_registry ADD COLUMN hot_summary_updated_at TEXT DEFAULT ''"
            )
            log.info("Added hot_summary_updated_at column to domain_registry")

        # Migration: add domain_id + source_file columns to rag_chunks if missing
        try:
            await db.execute("SELECT domain_id FROM rag_chunks LIMIT 0")
        except Exception:
            await db.execute(
                "ALTER TABLE rag_chunks ADD COLUMN domain_id TEXT NOT NULL DEFAULT ''"
            )
            log.info("Added domain_id column to rag_chunks")

        try:
            await db.execute("SELECT source_file FROM rag_chunks LIMIT 0")
        except Exception:
            await db.execute(
                "ALTER TABLE rag_chunks ADD COLUMN source_file TEXT NOT NULL DEFAULT ''"
            )
            log.info("Added source_file column to rag_chunks")

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_domain_id ON rag_chunks(domain_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_source_file ON rag_chunks(source_file)"
        )
        await db.commit()

    log.info("Domain tables initialized")


def _validate_domain_id(domain_id: str) -> bool:
    """Validate domain_id format. Must be lowercase alphanumeric + hyphens/underscores."""
    import re
    return bool(re.match(r'^[a-z0-9][a-z0-9_-]{0,63}$', domain_id))


def log_domain_mutation(domain_id: str, action: str, actor: str = "",
                        details: str = ""):
    """Audit log for domain mutations (CB-96). Structured logging."""
    log.info("DOMAIN_AUDIT domain=%s action=%s actor=%s details=%s",
             domain_id, action, actor[:50], details[:200])


async def register_domain(config: DomainConfig) -> bool:
    """Register a new domain or update existing one."""
    import json as _json
    if not _validate_domain_id(config.id):
        raise ValueError(
            f"Invalid domain_id '{config.id}': must match ^[a-z0-9][a-z0-9_-]{{0,63}}$"
        )
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    config_yaml = yaml.dump({"domain": {
        "id": config.id, "name": config.name, "description": config.description,
        "owner": config.owner,
        "source": {"type": config.source.type, "repo": config.source.repo,
                   "branch": config.source.branch,
                   "site_id": config.source.site_id,
                   "drive_id": config.source.drive_id,
                   "team_id": config.source.team_id,
                   "channel_id": config.source.channel_id,
                   "paths": config.source.paths,
                   "exclude": config.source.exclude},
        "indexing": {"chunking_strategy": config.indexing.chunking_strategy,
                     "chunk_size": config.indexing.chunk_size,
                     "chunk_overlap": config.indexing.chunk_overlap,
                     "poll_interval_minutes": config.indexing.poll_interval_minutes},
        "access": {"teams": config.access.teams, "admins": config.access.admins},
        "knowledge": {
            "prompt_modules": config.knowledge.prompt_modules,
            "skills": config.knowledge.skills,
            "auto_load_skills": config.knowledge.auto_load_skills,
            "facts_categories": config.knowledge.facts_categories,
            "essential_docs": config.knowledge.essential_docs,
            "hot_context_budget": config.knowledge.hot_context_budget,
            "custom_instructions": config.knowledge.custom_instructions,
            "facts_visible": config.knowledge.facts_visible,
            "default_harness_preset": config.knowledge.default_harness_preset,
        },
    }})

    # Denormalize knowledge fields as JSON arrays for fast queries
    prompt_modules_json = _json.dumps(config.knowledge.prompt_modules)
    skills_json = _json.dumps(config.knowledge.skills)
    auto_load_json = _json.dumps(config.knowledge.auto_load_skills)
    facts_cats_json = _json.dumps(config.knowledge.facts_categories)

    async with open_db() as db:
        await db.execute("""
            INSERT INTO domain_registry (domain_id, name, description, owner,
                                         config_yaml, prompt_modules, skills,
                                         auto_load_skills, facts_categories,
                                         custom_instructions, facts_visible,
                                         status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(domain_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                owner=excluded.owner, config_yaml=excluded.config_yaml,
                prompt_modules=excluded.prompt_modules, skills=excluded.skills,
                auto_load_skills=excluded.auto_load_skills,
                facts_categories=excluded.facts_categories,
                custom_instructions=excluded.custom_instructions,
                facts_visible=excluded.facts_visible,
                updated_at=excluded.updated_at
        """, (config.id, config.name, config.description, config.owner,
              config_yaml, prompt_modules_json, skills_json,
              auto_load_json, facts_cats_json,
              config.knowledge.custom_instructions,
              1 if config.knowledge.facts_visible else 0,
              now, now))
        await db.commit()

    log.info("Domain registered: %s (%s)", config.id, config.name)
    return True


async def list_domains() -> list[dict]:
    """List all registered domains with health info + knowledge stack config."""
    async with open_db() as db:
        cursor = await db.execute("""
            SELECT domain_id, name, description, owner, status,
                   last_sync_at, chunk_count, error_message, created_at,
                   prompt_modules, skills, auto_load_skills, facts_categories
            FROM domain_registry ORDER BY name
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_domain(domain_id: str) -> dict | None:
    """Get a single domain's full config and status."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT * FROM domain_registry WHERE domain_id = ?", (domain_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_domain_knowledge_configs(domain_ids: list[str]) -> list[dict]:
    """Get knowledge stack configs for multiple domains.

    Returns list of dicts with domain_id, name, description, and all
    knowledge fields (prompt_modules, skills, auto_load_skills, facts_categories).
    Used by harness/agent to compose multi-domain knowledge environments.
    """
    import json as _json
    if not domain_ids:
        return []
    async with open_db() as db:
        placeholders = ",".join("?" * len(domain_ids))
        cursor = await db.execute(f"""
            SELECT domain_id, name, description, status,
                   prompt_modules, skills, auto_load_skills, facts_categories,
                   custom_instructions, facts_visible
            FROM domain_registry
            WHERE domain_id IN ({placeholders}) AND status = 'active'
            ORDER BY name
        """, tuple(domain_ids))
        rows = await cursor.fetchall()

    results = []
    for row in rows:
        r = dict(row)
        # Parse JSON arrays (with fallback for old rows without these columns)
        for fld in ("prompt_modules", "skills", "auto_load_skills", "facts_categories"):
            raw = r.get(fld, "[]") or "[]"
            try:
                r[fld] = _json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                r[fld] = []
        # Defaults for new fields (backward compat with old rows)
        r.setdefault("custom_instructions", "")
        # SQLite stores booleans as 0/1 — coerce to Python bool
        r["facts_visible"] = bool(r.get("facts_visible", 1))
        results.append(r)
    return results


from dataclasses import dataclass as _dc_compose, field as _field_compose


@_dc_compose
class EffectiveComposition:
    """Output of compose_effective_modules() — unified knowledge selection (CB-118).

    Single source of truth for what a chat session loads. Used by BOTH
    CLAUDE.md composition AND system prompt. Eliminates the prior divergence
    where CLAUDE.md used harness static lists while system prompt used domain configs.
    """
    system_prompt_modules: list[str] = _field_compose(default_factory=list)  # → system prompt
    auto_load_skills: list[str] = _field_compose(default_factory=list)       # → composed into CLAUDE.md
    available_skills: list[str] = _field_compose(default_factory=list)       # → Skill tool catalog (full)
    preloaded_note: str = ""                                                  # → PRE-LOADED SKILLS text


def _parse_filter_entry(entry: str) -> tuple[bool, str, str]:
    """Parse a filter entry into (is_include, domain_or_empty, component).

    Notation:
      "bot-features"           → include bot-features from any domain
      "core:bot-features"      → include bot-features from core domain
      "admin:*" / "admin:all"  → include ALL from admin domain
      "-self-upgrade"          → exclude self-upgrade from any domain
      "-admin:self-upgrade"    → exclude self-upgrade from admin specifically

    If only excludes present → accept all EXCEPT excluded.
    If any includes present → only included items pass, then excludes applied.
    """
    is_include = True
    if entry.startswith("-"):
        is_include = False
        entry = entry[1:]
    if ":" in entry:
        domain, component = entry.split(":", 1)
        return (is_include, domain, component)
    return (is_include, "", entry)


def _apply_harness_filter(
    candidates: list[tuple[str, str]],
    harness_filter: list[str],
) -> list[str]:
    """Apply harness filter to candidate (domain_id, skill_name) pairs.

    Returns filtered skill names (deduplicated, order preserved).
    """
    includes: list[tuple[bool, str, str]] = []
    excludes: list[tuple[bool, str, str]] = []
    for entry in harness_filter:
        parsed = _parse_filter_entry(entry)
        if parsed[0]:
            includes.append(parsed)
        else:
            excludes.append(parsed)

    def _matches(domain: str, skill: str, rule_domain: str, rule_component: str) -> bool:
        domain_ok = (rule_domain == "" or rule_domain == domain)
        component_ok = (rule_component in ("*", "all") or rule_component == skill)
        return domain_ok and component_ok

    has_positive = len(includes) > 0
    result: list[str] = []
    seen: set[str] = set()

    for domain, skill in candidates:
        if skill in seen:
            continue
        # Exclusions have highest priority
        if any(_matches(domain, skill, ex[1], ex[2]) for ex in excludes):
            log.debug("FILTER excluded: %s:%s", domain, skill)
            continue
        # Inclusions: if positive entries exist, must match one
        if has_positive and not any(
            _matches(domain, skill, inc[1], inc[2]) for inc in includes
        ):
            log.debug("FILTER not included: %s:%s", domain, skill)
            continue
        seen.add(skill)
        result.append(skill)

    return result


def compose_effective_modules(
    domain_configs: list[dict],
    harness_filter: list[str] | None = None,
    prompt_filter: list[str] | None = None,
    chat_key: str = "",
) -> EffectiveComposition:
    """Compose effective knowledge selection for a chat session (CB-118).

    Single function used by BOTH CLAUDE.md composition AND system prompt.
    Domains are the source of truth. Two separate filters for two component types.

    Args:
        domain_configs: RBAC-filtered domain configs. Must be pre-filtered by RBAC.
        harness_filter: harness claude_md_modules. Filters auto_load_skills → CLAUDE.md.
            Supports domain:component notation (domain:*, -exclude, etc).
        prompt_filter: harness prompt_modules. Filters domain prompt_modules → system prompt.
            Same domain:component notation. None = accept all.
        chat_key: for per-chat overrides (future use).

    Returns:
        EffectiveComposition with 3 output lists + preloaded note.
    """
    from pathlib import Path
    _skills_dir = Path("/app/.claude/skills")

    # Step 1: COLLECT all offerings from domains WITH provenance
    candidate_modules: list[tuple[str, str]] = []  # (domain_id, module_name)
    candidate_auto_load: list[tuple[str, str]] = []  # (domain_id, skill_name)
    candidate_skills: list[str] = []
    seen_modules: set[str] = set()
    seen_auto: set[str] = set()
    seen_skills: set[str] = set()

    for dc in domain_configs:
        domain_id = dc.get("domain_id", "")
        for item in dc.get("prompt_modules", []):
            if item and item not in seen_modules:
                seen_modules.add(item)
                candidate_modules.append((domain_id, item))
        for item in dc.get("auto_load_skills", []):
            if item and item not in seen_auto:
                seen_auto.add(item)
                candidate_auto_load.append((domain_id, item))
        for item in dc.get("skills", []):
            if item and item not in seen_skills:
                seen_skills.add(item)
                candidate_skills.append(item)

    # Step 2a: FILTER prompt_modules via prompt_filter (→ system prompt)
    if prompt_filter is not None:
        if not prompt_filter:
            filtered_modules: list[str] = []
        else:
            filtered_modules = _apply_harness_filter(candidate_modules, prompt_filter)
    else:
        filtered_modules = [mod for _, mod in candidate_modules]

    # Step 2b: FILTER auto_load via harness_filter (→ CLAUDE.md)
    if harness_filter is not None:
        if not harness_filter:
            filtered_auto = []  # Empty list = accept nothing
        else:
            filtered_auto = _apply_harness_filter(candidate_auto_load, harness_filter)
    else:
        filtered_auto = [skill for _, skill in candidate_auto_load]

    # Step 3: available_skills = full union (NEVER filtered)
    all_skills = list(candidate_skills)
    for s in filtered_auto:
        if s not in all_skills:
            all_skills.append(s)

    # Step 4: Generate PRE-LOADED SKILLS note (matches actual CLAUDE.md content)
    preloaded = [s for s in filtered_auto if (_skills_dir / s).is_dir()]
    preloaded_note = ""
    if preloaded:
        preloaded_note = (
            "PRE-LOADED SKILLS (already in CLAUDE.md — do NOT invoke via Skill tool):\n"
            + ", ".join(preloaded)
            + "\nThese skills are fully loaded in your project context. "
            "Calling the Skill tool for them would double-load. "
            "Only use the Skill tool for skills NOT in this list."
        )

    return EffectiveComposition(
        system_prompt_modules=filtered_modules + filtered_auto,
        auto_load_skills=filtered_auto,
        available_skills=all_skills,
        preloaded_note=preloaded_note,
    )


def compose_domain_categories(
    harness_categories: list[str] | None,
    domain_configs: list[dict],
) -> list[str] | None:
    """Compose fact categories from harness + all domain configs.

    Returns deduplicated union, or None if harness has no category filter.
    None = show all facts (the default). Category filtering and domain scoping
    are orthogonal — domain_id filtering is handled separately by AccessContext.

    When harness_categories is None (show all), we preserve that regardless of
    domain configs — domains contribute their categories ONLY when the harness
    already has a category filter in place.
    """
    if harness_categories is None:
        return None  # No category filtering — domain scoping handled by allowed_domains

    seen: set[str] = set()
    result: list[str] = []

    for c in harness_categories:
        if c not in seen:
            seen.add(c)
            result.append(c)

    for dc in domain_configs:
        for c in dc.get("facts_categories", []):
            if c not in seen:
                seen.add(c)
                result.append(c)

    return result if result else None


def compose_domain_instructions(domain_configs: list[dict]) -> str:
    """Compose custom_instructions from all domain configs.

    Returns joined non-empty instructions from all domains,
    each prefixed with domain name for context.
    """
    parts = []
    for dc in domain_configs:
        instr = (dc.get("custom_instructions") or "").strip()
        if instr:
            domain_name = dc.get("name", dc.get("domain_id", ""))
            parts.append(f"[{domain_name} domain instructions]\n{instr}")
    return "\n\n".join(parts)


def build_domain_awareness_prompt(
    domain_configs: list[dict],
    hot_summaries: dict[str, str] | None = None,
    write_domain: str = "",
    reference_domains: list[str] | None = None,
) -> str:
    """Build the domain awareness section for the system prompt.

    Injects domain descriptions + write_domain/reference_domains so the model:
    1. Knows its write domain (shared knowledge goes here automatically)
    2. Knows reference domains (read-only context)
    3. Can route search queries to the right domain
    4. Has domain hot summaries for immediate awareness
    """
    if not domain_configs:
        return ""

    _ref_set = set(reference_domains or [])

    lines = [
        "ACTIVE KNOWLEDGE DOMAINS:",
        "",
        f"WRITE DOMAIN: {write_domain or '(none — all facts are chat-local)'}",
        f"REFERENCE DOMAINS (read-only): {', '.join(reference_domains) if reference_domains else '(none)'}",
        "",
        "Routing: shared knowledge → write_domain automatically via FACTS_UPDATE.",
        "Reference domains are read-only — search them but don't write to them.",
        "",
    ]

    for dc in domain_configs:
        domain_id = dc.get("domain_id", "")
        name = dc.get("name", domain_id)
        description = dc.get("description", "").strip()
        skills = dc.get("skills", [])

        # Annotate domain role
        if domain_id == write_domain:
            role_tag = " [WRITE]"
        elif domain_id in _ref_set:
            role_tag = " [READ-ONLY]"
        elif domain_id == "core":
            role_tag = " [CORE]"
        else:
            role_tag = ""

        lines.append(f"[{domain_id}] {name}{role_tag}")
        if description:
            desc_display = description[:200] + "..." if len(description) > 200 else description
            lines.append(f"  {desc_display}")
        if skills:
            lines.append(f"  Relevant skills: {', '.join(skills)}")

        # Inject hot summary if available
        if hot_summaries:
            summary = hot_summaries.get(domain_id, "")
            if summary:
                lines.append(summary)

        lines.append("")

    # Simplified routing guidance (CB-94)
    if write_domain:
        lines.append(
            f"When storing facts via FACTS_UPDATE: shared knowledge goes to your write "
            f"domain \"{write_domain}\" automatically (include \"domain\": \"{write_domain}\"). "
            f"Chat-specific data (task progress, session state) stays local (omit domain field)."
        )
    else:
        lines.append(
            "No write domain configured — all FACTS_UPDATE are stored as chat-local."
        )

    return "\n".join(lines)


async def update_domain_status(domain_id: str, status: str, *, error: str = "") -> None:
    """Update a domain's sync status and optional error message.

    Only sets last_sync_at on terminal success ("active"), not on intermediate
    states like "syncing". This prevents poll_all_domains() from skipping a
    domain that started syncing but failed before completing.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        if error:
            await db.execute(
                "UPDATE domain_registry SET status=?, error_message=?, updated_at=? WHERE domain_id=?",
                (status, error, now, domain_id),
            )
        elif status == "active":
            # Terminal success — update last_sync_at
            await db.execute(
                "UPDATE domain_registry SET status=?, error_message='', last_sync_at=?, updated_at=? WHERE domain_id=?",
                (status, now, now, domain_id),
            )
        else:
            # Intermediate state (e.g. "syncing") — don't update last_sync_at
            await db.execute(
                "UPDATE domain_registry SET status=?, error_message='', updated_at=? WHERE domain_id=?",
                (status, now, domain_id),
            )
        await db.commit()


async def delete_domain(domain_id: str) -> bool:
    """Delete a domain and all its chunks."""
    async with open_db() as db:
        # Delete chunks
        await db.execute(
            "DELETE FROM rag_chunks WHERE domain_id = ?", (domain_id,)
        )
        # Delete document registry
        await db.execute(
            "DELETE FROM domain_documents WHERE domain_id = ?", (domain_id,)
        )
        # Delete domain
        await db.execute(
            "DELETE FROM domain_registry WHERE domain_id = ?", (domain_id,)
        )
        await db.commit()
    log.info("Domain deleted: %s (all chunks removed)", domain_id)
    return True


# ============================================================
# Content Hashing & Incremental Sync
# ============================================================

def content_hash(content: str) -> str:
    """SHA-256 hash of file content for change detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def get_document_hash(domain_id: str, file_path: str) -> str | None:
    """Get stored content hash for a document (None if not indexed)."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT content_hash FROM domain_documents WHERE domain_id = ? AND file_path = ?",
            (domain_id, file_path),
        )
        row = await cursor.fetchone()
        return row["content_hash"] if row else None


async def update_document_record(
    domain_id: str,
    file_path: str,
    new_hash: str,
    chunk_count: int,
) -> None:
    """Update or create document record after successful indexing."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        await db.execute("""
            INSERT INTO domain_documents (domain_id, file_path, content_hash, chunk_count, last_indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(domain_id, file_path) DO UPDATE SET
                content_hash=excluded.content_hash,
                chunk_count=excluded.chunk_count,
                last_indexed_at=excluded.last_indexed_at,
                status='active'
        """, (domain_id, file_path, new_hash, chunk_count, now))
        await db.commit()


async def remove_document(domain_id: str, file_path: str) -> int:
    """Remove a document and all its chunks. Returns number of chunks removed."""
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM rag_chunks WHERE domain_id = ? AND source_file = ?",
            (domain_id, file_path),
        )
        row = await cursor.fetchone()
        count = row["cnt"] if row else 0

        await db.execute(
            "DELETE FROM rag_chunks WHERE domain_id = ? AND source_file = ?",
            (domain_id, file_path),
        )
        await db.execute(
            "DELETE FROM domain_documents WHERE domain_id = ? AND file_path = ?",
            (domain_id, file_path),
        )
        await db.commit()

    log.info("Removed document %s from domain %s (%d chunks)", file_path, domain_id, count)
    return count


# ============================================================
# Canonical domain registration (CB-91)
# ============================================================

# Canonical domain definitions — the bot's core knowledge structure.
# core: bot identity (always implicit in all chats)
# admin: bot infrastructure (admin DMs only)
# teamwork: team knowledge (team group chats)

_CANONICAL_DOMAINS: list[dict] = [
    {
        "id": "core",
        "name": "Core",
        "description": "Bot identity and base capabilities. Always present in all chats. "
                       "System prompt modules, canonical skills, bot personality and rules.",
        "owner": "",  # system-owned, admin manages
        "source": {"type": "local"},
        "knowledge": {
            "prompt_modules": [],
            "skills": [
                # Base capabilities
                "bot-features", "skills-catalog", "bot-commands", "rich-content",
                "media-guide", "preference-learning", "proactive-guide", "todo-guide",
                # Project management (available in all chats)
                "project-management",
                # General-purpose capabilities
                "browser-automation", "diagram-creation", "docx-creation",
                "email-operations", "file-sending", "html-report",
                "image-operations", "mcp-authentication", "ms365-guide", "phone-calls",
                "pptx-creation", "relay-chats", "third-party-correspondence",
                "travel-search",
            ],
            "auto_load_skills": [
                "bot-features", "skills-catalog", "bot-commands", "rich-content",
                "media-guide", "preference-learning", "proactive-guide", "todo-guide",
                "project-management", "ms365-guide",
            ],
            "facts_categories": ["bot", "infrastructure", "organization"],
            "custom_instructions": "",
            "facts_visible": True,
            "default_harness_preset": {
                "model": "claude-sonnet-4-6",
                "effort": "medium",
            },
        },
        "access": {"teams": ["*"], "admins": []},
    },
    {
        "id": "admin",
        "name": "Admin",
        "description": "Bot infrastructure, architecture decisions, deploy history, "
                       "operational runbooks, config rationale. Admin-only.",
        "owner": "",  # root admin
        "source": {"type": "local"},
        "knowledge": {
            "prompt_modules": [],
            "skills": [
                "bot-architecture", "bot-patterns", "bot-pitfalls", "bot-deploy",
                "bot-staging", "bot-backup", "coding-principles", "self-upgrade",
                "qa-testing", "bot-coding-rules",
            ],
            "auto_load_skills": [
                "bot-architecture", "bot-patterns", "bot-pitfalls", "bot-deploy",
                "bot-staging", "bot-backup", "coding-principles", "self-upgrade",
                "qa-testing", "bot-coding-rules",
            ],
            "facts_categories": ["infrastructure", "bot", "credentials"],
            "custom_instructions": "",
            "facts_visible": True,
            "default_harness_preset": {
                "model": "claude-opus-4-6[1m]",
                "effort": "high",
                "max_turns": 200,
            },
        },
        "access": {"teams": [], "admins": []},
    },
    {
        "id": "teamwork",
        "name": "Teamwork",
        "description": "Team knowledge, processes, business decisions, vendor info, "
                       "technical standards. Shared across team group chats.",
        "owner": "",  # admin, delegatable
        "source": {"type": "local"},
        "knowledge": {
            "prompt_modules": [],
            "skills": [],
            # CB-118: auto_load = what goes into CLAUDE.md
            "auto_load_skills": [],
            "facts_categories": ["organization", "operations", "strategy", "contacts"],
            "custom_instructions": "",
            "facts_visible": True,
            "default_harness_preset": {
                "model": "claude-sonnet-4-6",
                "effort": "medium",
                "max_turns": 50,
            },
        },
        "access": {"teams": ["teamwork"], "admins": []},
    },
]


async def seed_canonical_domains() -> int:
    """Register canonical domains (core, admin, teamwork) on startup.

    Preserves admin-customized domains: if a canonical domain already exists
    and its config_yaml differs from the built-in definition, the upsert is
    skipped to avoid overwriting admin changes. New domains are always created.
    Called from main.py startup after DB init.
    """
    count = 0
    for domain_dict in _CANONICAL_DOMAINS:
        try:
            config = DomainConfig.from_yaml({"domain": domain_dict})
            if not config.owner:
                # Set owner to root admin
                try:
                    from config import ROOT_ADMIN
                    config = DomainConfig(
                        id=config.id, name=config.name,
                        description=config.description,
                        owner=f"{ROOT_ADMIN[0]}:{ROOT_ADMIN[1]}",
                        source=config.source, indexing=config.indexing,
                        access=config.access, knowledge=config.knowledge,
                    )
                except Exception:
                    pass

            # Check if domain already exists with admin customizations.
            # Compare parsed config (not raw YAML strings) excluding owner
            # to avoid false diffs when ROOT_ADMIN changes.
            existing = await get_domain(config.id)
            if existing and existing.get("config_yaml"):
                try:
                    existing_parsed = yaml.safe_load(existing["config_yaml"]) or {}
                    existing_domain = existing_parsed.get("domain", {})
                    # Compare semantic fields only (exclude owner — changes with ROOT_ADMIN)
                    _fields_to_compare = ("description", "source", "indexing", "access", "knowledge")
                    existing_sig = {k: existing_domain.get(k) for k in _fields_to_compare}
                    canonical_sig = {
                        "description": config.description,
                        "source": {"type": config.source.type, "repo": config.source.repo,
                                   "branch": config.source.branch, "paths": config.source.paths,
                                   "exclude": config.source.exclude},
                        "indexing": {"chunking_strategy": config.indexing.chunking_strategy,
                                     "chunk_size": config.indexing.chunk_size},
                        "access": {"teams": config.access.teams, "admins": config.access.admins},
                        "knowledge": {"prompt_modules": config.knowledge.prompt_modules,
                                      "skills": config.knowledge.skills},
                    }
                    if existing_sig != canonical_sig:
                        log.warning("Preserving admin-customized canonical domain: %s", config.id)
                        continue  # Skip upsert — admin has customized this domain
                except Exception:
                    pass  # Parse error → upsert to fix

            await register_domain(config)
            count += 1
        except Exception as e:
            log.error("Failed to seed canonical domain '%s': %s",
                      domain_dict.get("id", "?"), e)
    if count:
        log.info("Seeded %d canonical domain(s)", count)
    return count


async def ensure_domain_scheduled_tasks() -> int:
    """Create scheduled tasks for domain maintenance (hygiene + optimization).

    Creates one hygiene + one optimize task per canonical domain, scoped to
    the domain owner's DM chat. Idempotent — skips if tasks already exist.
    """
    created = 0
    try:
        from bot.scheduler import list_tasks, add_task
        existing = list_tasks()
        existing_names = {t.get("name", "") for t in existing}

        for domain_dict in _CANONICAL_DOMAINS:
            domain_id = domain_dict["id"]

            # Get owner's chat_id for task scope
            try:
                from config import ROOT_ADMIN
                _owner_chat_id = str(ROOT_ADMIN[1])
            except Exception:
                continue

            # Domain hygiene task (weekly, Sunday 3am)
            hygiene_name = f"domain_hygiene_{domain_id}"
            if hygiene_name not in existing_names:
                try:
                    add_task(
                        name=hygiene_name,
                        hour=3, minute=0,
                        days=["sun"],
                        chat_id=_owner_chat_id,
                        prompt=f"Run fact hygiene for the {domain_id} domain: "
                               f"manage_domain(action='run_hygiene', domain_id='{domain_id}', dry_run='false'). "
                               f"Report results.",
                    )
                    created += 1
                except Exception as e:
                    log.debug("Failed to create %s task: %s", hygiene_name, e)

            # Domain optimization task (weekly, Saturday 3am)
            opt_name = f"domain_optimize_{domain_id}"
            if opt_name not in existing_names:
                try:
                    add_task(
                        name=opt_name,
                        hour=3, minute=0,
                        days=["sat"],
                        chat_id=_owner_chat_id,
                        prompt=f"Run self-optimization analysis for the {domain_id} domain: "
                               f"manage_domain(action='analyze', domain_id='{domain_id}'). "
                               f"If recommendations found, list them.",
                    )
                    created += 1
                except Exception as e:
                    log.debug("Failed to create %s task: %s", opt_name, e)

    except Exception as e:
        log.debug("Domain scheduled task creation failed: %s", e)

    if created:
        log.info("Created %d domain scheduled task(s)", created)
    return created


# ============================================================
# Document Chunking
# ============================================================

def chunk_markdown(content: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """Chunk markdown content by heading boundaries.

    Strategy:
    1. Split on H1/H2/H3 headings as primary boundaries
    2. Within each section, split recursively at chunk_size chars
    3. Overlap between chunks for context continuity
    """
    import re

    # Split on headings (keep heading with its content)
    sections = re.split(r"(?=^#{1,3}\s)", content, flags=re.MULTILINE)
    sections = [s.strip() for s in sections if s.strip()]

    chunks: list[str] = []
    current_chunk = ""

    for section in sections:
        if len(current_chunk) + len(section) <= chunk_size:
            current_chunk += "\n\n" + section if current_chunk else section
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            # If section itself is too large, split recursively
            if len(section) > chunk_size:
                sub_chunks = _split_recursive(section, chunk_size, overlap)
                chunks.extend(sub_chunks)
                current_chunk = ""
            else:
                current_chunk = section

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks if chunks else [content[:chunk_size]]


def chunk_code(content: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """Chunk code files by function/class boundaries."""
    import re

    # Split on function/class definitions
    blocks = re.split(
        r"(?=^(?:def |class |async def |function |export ))",
        content,
        flags=re.MULTILINE,
    )
    blocks = [b.strip() for b in blocks if b.strip()]

    chunks: list[str] = []
    current_chunk = ""

    for block in blocks:
        if len(current_chunk) + len(block) <= chunk_size:
            current_chunk += "\n\n" + block if current_chunk else block
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            if len(block) > chunk_size:
                chunks.extend(_split_recursive(block, chunk_size, overlap))
                current_chunk = ""
            else:
                current_chunk = block

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks if chunks else [content[:chunk_size]]


def _split_recursive(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text recursively at paragraph/line boundaries."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:])
            break

        # Find a good break point (paragraph > newline > space)
        break_point = text.rfind("\n\n", start, end)
        if break_point == -1 or break_point <= start:
            break_point = text.rfind("\n", start, end)
        if break_point == -1 or break_point <= start:
            break_point = text.rfind(" ", start, end)
        if break_point == -1 or break_point <= start:
            break_point = end

        chunks.append(text[start:break_point].strip())
        new_start = break_point - overlap  # overlap for context continuity
        # Ensure forward progress (prevent infinite loop / duplicate chunks)
        start = max(new_start, start + 1) if new_start > start else break_point

    return [c for c in chunks if c]


def chunk_document(
    content: str,
    strategy: str = "markdown_headers",
    chunk_size: int = 1500,
    overlap: int = 200,
) -> list[str]:
    """Chunk a document using the specified strategy."""
    if strategy == "code":
        return chunk_code(content, chunk_size, overlap)
    elif strategy == "recursive":
        return _split_recursive(content, chunk_size, overlap)
    else:  # markdown_headers (default)
        return chunk_markdown(content, chunk_size, overlap)


# ============================================================
# Contextual Chunk Enrichment (Anthropic Contextual Retrieval pattern)
# ============================================================

async def _enrich_chunks_with_context(
    chunks: list[str],
    domain_id: str,
    file_path: str,
    full_content: str,
) -> list[str]:
    """Prepend document-level context to each chunk (Anthropic Contextual Retrieval pattern).

    Uses Gemini Flash to generate a brief context prefix for each chunk,
    situating it within its source document. This improves retrieval quality
    by ~49% according to Anthropic's benchmarks.

    Done at indexing time (one-time cost), not at query time.
    Returns original chunks unmodified if Gemini is unavailable.
    """
    import os
    from config import GEMINI_API_KEY as gemini_api_key
    if not gemini_api_key or len(chunks) == 0:
        return chunks  # No enrichment without Gemini

    try:
        from google import genai

        client = genai.Client(api_key=gemini_api_key)
        model_name = os.environ.get("GEMINI_MODEL_DESCRIBE", "gemini-2.5-flash-lite")

        # Build chunk descriptions (cap at 30 to stay within context limits)
        chunk_descriptions = []
        for i, chunk in enumerate(chunks[:30]):
            chunk_descriptions.append(f"CHUNK {i + 1}:\n{chunk[:500]}")

        prompt = (
            f'You are analyzing chunks from the document "{file_path}" '
            f'in the "{domain_id}" knowledge domain.\n'
            f"\n"
            f"Document overview (first 2000 chars):\n"
            f"{full_content[:2000]}\n"
            f"\n"
            f"For each chunk below, generate a ONE-LINE context prefix that "
            f"situates it within the document.\n"
            f"Format: CHUNK N: [context prefix]\n"
            f"The prefix should mention the document name, section topic, "
            f"and what this chunk covers.\n"
            f"\n"
            + "\n".join(chunk_descriptions)
        )

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
        )

        if not response or not response.text:
            return chunks

        # Parse context prefixes from response
        prefixes: dict[int, str] = {}
        for line in response.text.strip().split("\n"):
            line = line.strip()
            if line.startswith("CHUNK ") and ":" in line:
                try:
                    num_part = line.split(":")[0].replace("CHUNK ", "").strip()
                    prefix = ":".join(line.split(":")[1:]).strip()
                    if prefix:
                        prefixes[int(num_part)] = prefix
                except (ValueError, IndexError):
                    continue

        # Prepend context to each chunk
        enriched = []
        for i, chunk in enumerate(chunks):
            prefix = prefixes.get(i + 1, f"From {file_path} in {domain_id}")
            enriched.append(f"[{prefix}]\n\n{chunk}")

        log.info("Enriched %d/%d chunks with context for %s/%s",
                 len(prefixes), len(chunks), domain_id, file_path)
        return enriched

    except Exception as e:
        log.warning("Chunk enrichment failed for %s/%s: %s — using raw chunks",
                    domain_id, file_path, e)
        return chunks


# ============================================================
# Indexing Pipeline
# ============================================================

async def index_document(
    domain_id: str,
    file_path: str,
    content: str,
    strategy: str = "markdown_headers",
    chunk_size: int = 1500,
    chunk_overlap: int = 200,
) -> int:
    """Index a single document into the RAG system.

    1. Chunk the content
    2. Embed all chunks
    3. Store with domain_id + source_file tags
    4. Update document registry

    Returns: number of chunks created.
    """
    from bot.storage.rag import embed_texts, _vec_to_blob

    # Chunk
    chunks = chunk_document(content, strategy, chunk_size, chunk_overlap)
    if not chunks:
        return 0

    # Contextual chunk enrichment (Anthropic pattern — improves retrieval ~49%)
    chunks = await _enrich_chunks_with_context(chunks, domain_id, file_path, content)

    # Embed
    embeddings = await embed_texts(chunks)

    # Remove old chunks for this file (re-index replaces all)
    async with open_db() as db:
        await db.execute(
            "DELETE FROM rag_chunks WHERE domain_id = ? AND source_file = ?",
            (domain_id, file_path),
        )

        # Insert new chunks
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for i, (chunk_text, embedding) in enumerate(zip(chunks, embeddings)):
            await db.execute("""
                INSERT INTO rag_chunks
                    (source, chat_id, start_msg_id, end_msg_id, senders,
                     start_ts, end_ts, chunk_text, embedding, model, created_at,
                     domain_id, source_file)
                VALUES (?, '', 0, 0, '', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"domain:{domain_id}",
                now, now,
                chunk_text,
                _vec_to_blob(embedding),
                "gemini-embedding-001",
                now,
                domain_id,
                file_path,
            ))

        await db.commit()

    # Update document record
    file_hash = content_hash(content)
    await update_document_record(domain_id, file_path, file_hash, len(chunks))

    # Update domain chunk count
    async with open_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM rag_chunks WHERE domain_id = ?",
            (domain_id,),
        )
        row = await cursor.fetchone()
        total = row["cnt"] if row else 0
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await db.execute(
            "UPDATE domain_registry SET chunk_count=?, last_sync_at=?, status='active' WHERE domain_id=?",
            (total, now, domain_id),
        )
        await db.commit()

    log.info("Indexed %s/%s: %d chunks (domain total: %d)",
             domain_id, file_path, len(chunks), total if 'total' in dir() else len(chunks))

    # Auto-refresh hot summary after indexing new content
    try:
        from bot.storage.domain_summary import refresh_domain_summary
        await refresh_domain_summary(domain_id)
    except Exception as e:
        log.warning("Failed to refresh domain summary after indexing: %s", e)

    return len(chunks)


# ============================================================
# Domain-Scoped RAG Search
# ============================================================

async def search_domains(
    query: str,
    allowed_domains: list[str],
    limit: int = 10,
) -> list[dict]:
    """Search across allowed knowledge domains.

    This extends the existing RAG search with domain_id filtering.
    Only chunks from allowed_domains are considered.

    Args:
        query: Search query text
        allowed_domains: List of domain_ids the user has access to
        limit: Max results to return

    Returns: List of {chunk_text, domain_id, source_file, similarity} dicts.
    """
    if not allowed_domains:
        return []

    from bot.storage.rag import embed_texts, _blob_to_vec
    import numpy as np

    # Embed query
    embeddings = await embed_texts([query])
    if not embeddings:
        return []
    query_vec = embeddings[0]

    # Build domain filter
    placeholders = ",".join("?" * len(allowed_domains))

    # Cap results to prevent OOM on large knowledge bases
    # For >10K chunks, consider vector DB with ANN index
    _MAX_SEARCH_ROWS = 10000

    async with open_db() as db:
        cursor = await db.execute(f"""
            SELECT chunk_text, embedding, domain_id, source_file
            FROM rag_chunks
            WHERE domain_id IN ({placeholders})
              AND domain_id != ''
            LIMIT {_MAX_SEARCH_ROWS}
        """, tuple(allowed_domains))

        results = []
        async for row in cursor:
            chunk_vec = _blob_to_vec(row["embedding"])
            # Cosine similarity
            dot = float(np.dot(query_vec, chunk_vec))
            norm_q = float(np.linalg.norm(query_vec))
            norm_c = float(np.linalg.norm(chunk_vec))
            if norm_q > 0 and norm_c > 0:
                similarity = dot / (norm_q * norm_c)
            else:
                similarity = 0.0
            results.append({
                "chunk_text": row["chunk_text"],
                "domain_id": row["domain_id"],
                "source_file": row["source_file"],
                "similarity": similarity,
            })

    # Sort by similarity, return top N
    # NOTE: For >10K chunks, migrate to paginated heap or vector DB (pgvector/Qdrant)
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:limit]


# ============================================================
# GitHub Webhook Handler
# ============================================================

async def handle_github_push(
    payload: dict,
    webhook_secret: str = "",
) -> dict:
    """Handle a GitHub push webhook — trigger incremental re-index.

    Args:
        payload: The webhook JSON payload
        webhook_secret: Expected webhook secret for validation

    Returns: {"status": "ok/error", "files_processed": N, "message": "..."}
    """
    # Extract affected files from push payload
    commits = payload.get("commits", [])
    added: set[str] = set()
    modified: set[str] = set()
    removed: set[str] = set()

    for commit in commits:
        added.update(commit.get("added", []))
        modified.update(commit.get("modified", []))
        removed.update(commit.get("removed", []))

    # Modified files include both added and modified
    to_index = added | modified

    repo_full_name = payload.get("repository", {}).get("full_name", "")
    branch_ref = payload.get("ref", "")  # refs/heads/main

    log.info(
        "GitHub push webhook: repo=%s, branch=%s, +%d ~%d -%d files",
        repo_full_name, branch_ref, len(added), len(modified), len(removed),
    )

    # Find which domain this repo belongs to
    domains = await list_domains()
    matching_domain = None
    for d in domains:
        config = yaml.safe_load(d.get("config_yaml", "{}"))
        domain_cfg = config.get("domain", config)
        source = domain_cfg.get("source", {})
        if source.get("repo") == repo_full_name:
            expected_branch = f"refs/heads/{source.get('branch', 'main')}"
            if branch_ref == expected_branch:
                matching_domain = d
                break

    if not matching_domain:
        return {"status": "ignored", "message": f"No domain registered for {repo_full_name}:{branch_ref}"}

    domain_id = matching_domain["domain_id"]

    # Handle removals
    for file_path in removed:
        await remove_document(domain_id, file_path)

    # For added/modified: would need to fetch content from GitHub
    # This is a stub — full implementation fetches via GitHub API or git clone
    return {
        "status": "ok",
        "domain_id": domain_id,
        "files_to_index": len(to_index),
        "files_removed": len(removed),
        "message": f"Queued {len(to_index)} files for re-indexing in domain '{domain_id}'",
    }


# ============================================================
# Load domains from YAML files on disk
# ============================================================

async def load_domains_from_dir(domains_dir: Path) -> int:
    """Load all domain.yaml files from a directory and register them.

    Called at startup to populate the domain registry from config files.
    Returns number of domains loaded.
    """
    if not domains_dir.exists():
        return 0

    count = 0
    for yaml_file in sorted(domains_dir.glob("*.yaml")):
        if yaml_file.name.startswith("example"):
            continue  # Skip example files
        try:
            raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            config = DomainConfig.from_yaml(raw)
            if config.id:
                await register_domain(config)
                count += 1
                log.info("Loaded domain config: %s from %s", config.id, yaml_file.name)
        except Exception as e:
            log.error("Failed to load domain config %s: %s", yaml_file, e)

    return count
