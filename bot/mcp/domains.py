# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Domain management MCP tools — knowledge domain CRUD + search.

Admin tools for managing knowledge domains:
- manage_domain: register, list, get, delete, reindex, health
- search_knowledge: query knowledge domains (permission-scoped)

Available to Bot Admin (Tier 1) and Domain Admin (Tier 2) users.
search_knowledge is available to all employees (scoped by harness allowed_domains).
"""

import logging

from claude_agent_sdk import tool

from bot.mcp import _run_with_timeout, _resolve_access_ctx, _safe_int

log = logging.getLogger(__name__)


@tool("manage_domain", "Manage knowledge domains. Actions: list (show all), get (detail), register (new), delete, reindex, sync (trigger full sync for any source type: GitHub or Graph API), health, refresh_summary.", {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "list | get | register | delete | reindex | health | refresh_summary"},
        "domain_id": {"type": "string", "description": "Domain ID (for get/delete/reindex/refresh_summary)"},
        "config_yaml": {"type": "string", "description": "YAML config string (for register)"},
    },
    "required": ["action"],
})
async def manage_domain(args: dict) -> dict:
    """Manage knowledge domains."""
    action = args.get("action", "")
    domain_id = args.get("domain_id", "")
    config_yaml = args.get("config_yaml", "")
    _session_key = args.pop("_session_key", "")

    async def _do():
        from bot.storage.domains import (
            list_domains, get_domain, register_domain, delete_domain,
            DomainConfig,
        )
        import yaml

        # Per-domain authorization: owner enforcement (CB-96)
        # Admin-only: structural changes to domain config
        _ADMIN_ACTIONS = frozenset({"register", "delete", "setup_deploy_key"})
        # Domain-write: content operations that users with domain write access can do
        _WRITE_ACTIONS = frozenset({"sync", "reindex", "refresh_summary", "export_facts", "run_hygiene", "analyze", "review_recommendation"})
        _GATED_ACTIONS = _ADMIN_ACTIONS | _WRITE_ACTIONS

        if action in _GATED_ACTIONS:
            from bot.rbac.engine import is_domain_admin
            # Resolve user from session
            user_id = ""
            is_admin = False
            if _session_key:
                from bot.core.session_state import _get_or_create_state
                state = _get_or_create_state(_session_key)
                ctx = state.get("current_user_ctx")
                if ctx:
                    user_id = ctx.get("user_id", "")
                    is_admin = ctx.get("is_admin", False)
            # Bot Admin can do anything
            if not is_admin:
                # Check if user is domain owner (CB-96)
                _is_owner = False
                if domain_id and action != "register":
                    _existing_domain = await get_domain(domain_id)
                    if _existing_domain:
                        _dc_yaml = _existing_domain.get("config_yaml", "")
                        if _dc_yaml:
                            try:
                                _dc_parsed = yaml.safe_load(_dc_yaml) or {}
                                _dc_owner = _dc_parsed.get("domain", {}).get("owner", "")
                                _is_owner = bool(_dc_owner and _dc_owner == user_id)
                            except Exception:
                                pass
                if not _is_owner:
                    # For write actions: check domain write access (not admin)
                    # For admin actions: require admin privilege
                    _required_priv = "write" if action in _WRITE_ACTIONS else "admin"
                    _required_obj = "domain_data" if action in _WRITE_ACTIONS else "knowledge_domain"
                    from bot.rbac import get_engine as _get_rbac_engine
                    from bot.rbac.models import Subject as _RBACSubject
                    _rbac_engine = _get_rbac_engine()
                    _rbac_dec = await _rbac_engine.check_access(
                        _RBACSubject(user_id=user_id),
                        _required_priv, _required_obj, domain_id or "*",
                        log_decision=False,
                    )
                    if not _rbac_dec.allowed:
                        if domain_id and action != "register":
                            if not await is_domain_admin(user_id, domain_id):
                                return {"error": f"Permission denied: you don't have {_required_priv} access to domain '{domain_id}'."}
                        elif action == "register":
                            return {"error": "Permission denied: only Bot Admin can register new domains."}

        if action == "list":
            domains = await list_domains()
            if not domains:
                return {"domains": [], "message": "No domains registered. Use register action with config_yaml."}
            import json as _json
            result = []
            for d in domains:
                entry = {
                    "id": d["domain_id"],
                    "name": d["name"],
                    "description": (d.get("description") or "")[:100],
                    "status": d["status"],
                    "chunks": d["chunk_count"],
                    "last_sync": d["last_sync_at"] or "never",
                    "error": d["error_message"] or None,
                }
                # Include knowledge stack fields if populated
                for kf in ("prompt_modules", "skills", "auto_load_skills", "facts_categories"):
                    raw = d.get(kf, "[]") or "[]"
                    try:
                        val = _json.loads(raw) if isinstance(raw, str) else raw
                    except (ValueError, TypeError):
                        val = []
                    if val:
                        entry[kf] = val
                result.append(entry)
            return {"domains": result, "count": len(result)}

        elif action == "get":
            if not domain_id:
                return {"error": "domain_id required for 'get' action"}
            d = await get_domain(domain_id)
            if not d:
                return {"error": f"Domain '{domain_id}' not found"}
            return d

        elif action == "register":
            if not config_yaml:
                return {"error": "config_yaml required for 'register' action. Provide YAML with domain config."}
            try:
                raw = yaml.safe_load(config_yaml)
                config = DomainConfig.from_yaml(raw)
                if not config.id:
                    return {"error": "Domain config must include 'id' field"}
                await register_domain(config)
                from bot.audit import log_admin_action
                await log_admin_action("domain.register", user_id, config.id, "success",
                                       {"name": config.name})
                return {"success": True, "domain_id": config.id, "name": config.name}
            except Exception as e:
                return {"error": f"Failed to parse/register domain: {e}"}

        elif action == "delete":
            if not domain_id:
                return {"error": "domain_id required for 'delete' action"}
            d = await get_domain(domain_id)
            if not d:
                return {"error": f"Domain '{domain_id}' not found"}
            await delete_domain(domain_id)
            from bot.audit import log_admin_action
            await log_admin_action("domain.delete", user_id, domain_id, "success",
                                   {"chunks_removed": d.get("chunk_count", 0)})
            return {"success": True, "deleted": domain_id, "chunks_removed": d.get("chunk_count", 0)}

        elif action == "reindex":
            if not domain_id:
                return {"error": "domain_id required for 'reindex' action"}
            d = await get_domain(domain_id)
            if not d:
                return {"error": f"Domain '{domain_id}' not found"}
            # Mark as syncing
            from bot.storage.db import open_db
            import time
            async with open_db() as db:
                await db.execute(
                    "UPDATE domain_registry SET status='syncing', updated_at=? WHERE domain_id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), domain_id),
                )
                await db.commit()
            from bot.audit import log_admin_action
            await log_admin_action("domain.reindex", user_id, domain_id, "success")
            return {
                "status": "reindex_queued",
                "domain_id": domain_id,
                "message": f"Full re-index queued for '{domain_id}'. "
                           f"Use GitHub webhook push or manual file indexing to populate.",
            }

        elif action == "health":
            domains = await list_domains()
            total_chunks = sum(d["chunk_count"] for d in domains)
            healthy = sum(1 for d in domains if d["status"] == "active")
            errored = sum(1 for d in domains if d["status"] == "error")
            return {
                "total_domains": len(domains),
                "healthy": healthy,
                "errored": errored,
                "total_chunks": total_chunks,
                "domains": [
                    {"id": d["domain_id"], "status": d["status"], "chunks": d["chunk_count"]}
                    for d in domains
                ],
            }

        elif action == "sync":
            if not domain_id:
                return {"error": "domain_id required for 'sync' action"}
            d = await get_domain(domain_id)
            if not d:
                return {"error": f"Domain '{domain_id}' not found"}

            # Determine source type and sync accordingly
            cfg = yaml.safe_load(d["config_yaml"])
            source_type = cfg.get("domain", cfg).get("source", {}).get("type", "github")

            if source_type == "graph_api":
                from bot.storage.graph_sync import sync_graph_domain
                result = await sync_graph_domain(domain_id)
            elif source_type == "github":
                # CB-131: Auto-route to SSH deploy key or REST API
                from bot.storage.github_sync import sync_domain_auto
                _source = cfg.get("domain", cfg).get("source", {})
                result = await sync_domain_auto(
                    domain_id,
                    _source.get("repo", ""),
                    _source.get("branch", "main"),
                )
            else:
                return {"error": f"Unknown source type: {source_type}"}

            return {"success": True, "domain_id": domain_id, "sync_result": result}

        elif action == "refresh_summary":
            if not domain_id:
                return {"error": "domain_id required for 'refresh_summary' action"}
            d = await get_domain(domain_id)
            if not d:
                return {"error": f"Domain '{domain_id}' not found"}
            from bot.storage.domain_summary import refresh_domain_summary
            summary = await refresh_domain_summary(domain_id)
            return {
                "success": True,
                "domain_id": domain_id,
                "summary_length": len(summary),
                "summary_preview": summary[:500],
            }

        elif action == "setup_deploy_key":
            # CB-99: Generate Ed25519 keypair, upload to GitHub, store encrypted
            if not domain_id:
                return {"error": "domain_id required for 'setup_deploy_key' action"}
            d = await get_domain(domain_id)
            if not d:
                return {"error": f"Domain '{domain_id}' not found"}
            # Parse domain config to get repo
            _dc = DomainConfig.from_yaml(yaml.safe_load(d.get("config_yaml", "{}") or "{}"))
            if not _dc.source.repo:
                return {"error": f"Domain '{domain_id}' has no source.repo configured"}

            from bot.storage.github_sync import _generate_ed25519_keypair
            from bot.storage.domains import log_domain_mutation
            private_pem, public_openssh = _generate_ed25519_keypair()

            # Upload public key to GitHub via API
            try:
                _gh_token = ""
                # CB-131: Try config env var first, then credential_store
                try:
                    import config as _cfg
                    _gh_token = getattr(_cfg, "GITHUB_TOKEN", "")
                except Exception:
                    pass
                if not _gh_token:
                    # Fallback: check github:system in credential_store
                    try:
                        from bot.auth.credentials import get_credential
                        _sys_cred = await get_credential("github:system", decrypt=True)
                        if _sys_cred:
                            _gh_token = (_sys_cred.get("data") or {}).get("access_token", "")
                    except Exception:
                        pass
                if not _gh_token:
                    return {"error": "No GitHub token available for deploy key upload. "
                            "Set GITHUB_TOKEN env var or store github:system credential."}

                import httpx
                async with httpx.AsyncClient(timeout=30) as _hc:
                    _resp = await _hc.post(
                        f"https://api.github.com/repos/{_dc.source.repo}/keys",
                        headers={
                            "Authorization": f"Bearer {_gh_token}",
                            "Accept": "application/vnd.github+json",
                        },
                        json={
                            "title": f"bot-deploy-key-{domain_id}",
                            "key": public_openssh,
                            "read_only": False,  # R/W per security model
                        },
                    )
                    if _resp.status_code not in (200, 201):
                        return {"error": f"GitHub API error {_resp.status_code}: {_resp.text[:200]}"}
                    _gh_key_data = _resp.json()
                    _gh_key_id = _gh_key_data.get("id", 0)
            except Exception as _gh_err:
                return {"error": f"Failed to upload deploy key to GitHub: {str(_gh_err)[:200]}"}

            # Store private key encrypted in credential_store
            try:
                from bot.auth.credentials import store_credential
                import hashlib
                _fingerprint = hashlib.sha256(public_openssh.encode()).hexdigest()[:16]
                _cred_id = f"ssh:deploy-key-{domain_id}"
                await store_credential(
                    cred_id=_cred_id,
                    cred_type="ssh-key",
                    account=_dc.source.repo,
                    scope="system",
                    owner=f"domain:{domain_id}",
                    data={"private_key": private_pem},
                    metadata={
                        "public_key_fingerprint": _fingerprint,
                        "github_deploy_key_id": _gh_key_id,
                        "domain_id": domain_id,
                        "created_by": user_id,
                        "read_only": False,
                    },
                )
            except Exception as _store_err:
                return {"error": f"Failed to store deploy key: {str(_store_err)[:200]}"}

            # Update domain source config with credential reference
            _dc.source.type = "github"
            # Store the cred_id in domain config for sync_domain to find
            # We use the config_yaml blob for this
            try:
                _dc_yaml = yaml.safe_load(d.get("config_yaml", "{}") or "{}")
                _dc_yaml.setdefault("domain", {}).setdefault("source", {})["ssh_deploy_key_cred_id"] = _cred_id
                from bot.storage.domains import register_domain
                _updated_config = DomainConfig.from_yaml(_dc_yaml)
                _updated_config.id = domain_id
                await register_domain(_updated_config)
            except Exception:
                pass  # Non-critical — cred_id stored in credential_store metadata

            log_domain_mutation(domain_id, "setup_deploy_key", actor=user_id,
                                details=f"repo={_dc.source.repo} fingerprint={_fingerprint}")

            return {
                "success": True,
                "domain_id": domain_id,
                "repo": _dc.source.repo,
                "fingerprint": _fingerprint,
                "github_key_id": _gh_key_id,
                "credential_id": _cred_id,
                "message": f"Deploy key created for {_dc.source.repo}. Stored as {_cred_id} (scope=system, encrypted).",
            }

        elif action == "export_facts":
            # CB-113: Export non-sensitive domain facts to YAML (+ optional GitHub push)
            if not domain_id:
                return {"error": "domain_id required for 'export_facts' action"}
            from bot.storage.fact_export import export_domain_facts, push_facts_to_github
            result = await export_domain_facts(domain_id)
            if result.get("yaml_content") and args.get("push"):
                d = await get_domain(domain_id)
                if d:
                    _dc = DomainConfig.from_yaml(yaml.safe_load(d.get("config_yaml", "{}") or "{}"))
                    if _dc.source.repo:
                        pushed = await push_facts_to_github(domain_id, result["yaml_content"], _dc.source.repo, _dc.source.branch)
                        result["pushed"] = pushed
            return result

        elif action == "run_hygiene":
            # CB-113: Run fact staleness detection + dedup
            if not domain_id:
                return {"error": "domain_id required for 'run_hygiene' action"}
            dry_run = str(args.get("dry_run", "true")).lower() != "false"
            from bot.storage.fact_hygiene import run_fact_hygiene
            return await run_fact_hygiene(domain_id=domain_id, dry_run=dry_run)

        elif action == "analyze":
            # CB-113: Run self-optimization analysis on domain
            if not domain_id:
                return {"error": "domain_id required for 'analyze' action"}
            days = int(args.get("days", 7))
            from bot.storage.domain_optimize import analyze_domain_chats
            return await analyze_domain_chats(domain_id, days=days)

        elif action == "list_recommendations":
            # CB-113: List domain recommendations
            if not domain_id:
                return {"error": "domain_id required for 'list_recommendations' action"}
            status_filter = args.get("status", "pending")
            from bot.storage.domain_optimize import list_recommendations
            recs = await list_recommendations(domain_id, status=status_filter)
            return {"recommendations": recs, "count": len(recs)}

        elif action == "review_recommendation":
            # CB-113: Approve/reject a recommendation
            rec_id = args.get("recommendation_id") or args.get("rec_id")
            if not rec_id:
                return {"error": "recommendation_id required for 'review_recommendation' action"}
            review_action = args.get("review_action", "approve")
            notes = args.get("notes", "")
            from bot.storage.domain_optimize import review_recommendation
            return await review_recommendation(int(rec_id), action=review_action, actor=user_id, notes=notes)

        else:
            return {"error": f"Unknown action: '{action}'. Valid: list, get, register, delete, reindex, sync, health, refresh_summary, setup_deploy_key, export_facts, run_hygiene, analyze, list_recommendations, review_recommendation"}

    return await _run_with_timeout(_do(), "manage_domain")


@tool("search_knowledge", "Search knowledge domains for information. Returns relevant chunks from registered knowledge bases, scoped to accessible domains.", {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query text"},
        "limit": {"type": "string", "description": "Max results (default 10)"},
        "domain_filter": {"type": "string", "description": "Comma-separated domain IDs to narrow search"},
    },
    "required": ["query"],
})
async def search_knowledge(args: dict) -> dict:
    """Search across knowledge domains."""
    query = args.get("query", "")
    limit = args.get("limit", "10")
    domain_filter = args.get("domain_filter", "")
    ctx = _resolve_access_ctx(args)

    async def _do():
        from bot.storage.domains import search_domains

        # Determine allowed domains from AccessContext
        if ctx and ctx.allowed_domains:
            allowed = list(ctx.allowed_domains)
        elif ctx and ctx.is_admin and not ctx.allowed_chat_ids:
            # Admin DM (global scope) with no domain restriction: search all
            from bot.storage.domains import list_domains
            domains = await list_domains()
            allowed = [d["domain_id"] for d in domains if d["status"] == "active"]
        else:
            return {"results": [], "message": "No knowledge domains accessible in this session."}

        # Apply optional domain_filter (must be subset of allowed)
        if domain_filter:
            requested = [d.strip() for d in domain_filter.split(",") if d.strip()]
            allowed = [d for d in requested if d in allowed]
            if not allowed:
                return {"results": [], "message": "Requested domains not in your access list."}

        if not allowed:
            return {"results": [], "message": "No domains to search."}

        max_results = _safe_int(limit, 10)
        results = await search_domains(query, allowed, limit=max_results)

        return {
            "results": [
                {
                    "text": r["chunk_text"][:500],  # truncate for response size
                    "domain": r["domain_id"],
                    "source": r["source_file"],
                    "relevance": round(r["similarity"], 3),
                }
                for r in results
            ],
            "total": len(results),
            "domains_searched": allowed,
        }

    return await _run_with_timeout(_do(), "search_knowledge")


@tool("index_document", "Index a document into a knowledge domain. Content is chunked, embedded, and stored for search.", {
    "type": "object",
    "properties": {
        "domain_id": {"type": "string", "description": "Target domain ID (must exist)"},
        "file_path": {"type": "string", "description": "Document path (e.g. 'runbooks/deploy.md')"},
        "content": {"type": "string", "description": "Full document content to index"},
        "strategy": {"type": "string", "description": "Chunking strategy: markdown_headers | code | recursive"},
    },
    "required": ["domain_id", "file_path", "content"],
})
async def index_document_tool(args: dict) -> dict:
    """Index a document into a knowledge domain."""
    domain_id = args.get("domain_id", "")
    file_path = args.get("file_path", "")
    content = args.get("content", "")
    strategy = args.get("strategy", "markdown_headers")
    _session_key = args.pop("_session_key", "")

    async def _do():
        from bot.storage.domains import get_domain, index_document

        # Authorization: require domain write access (Bot Admin, Domain Admin, or RBAC write)
        from bot.rbac.engine import is_domain_admin
        user_id = ""
        is_admin = False
        if _session_key:
            from bot.core.session_state import _get_or_create_state
            state = _get_or_create_state(_session_key)
            ctx = state.get("current_user_ctx")
            if ctx:
                user_id = ctx.get("user_id", "")
                is_admin = ctx.get("is_admin", False)
        if not is_admin:
            # Check RBAC write access on domain_data (teamwork users can index their domain)
            _has_write = False
            if domain_id:
                from bot.rbac import get_engine as _get_rbac_idx
                from bot.rbac.models import Subject as _RBACSubjectIdx
                _rbac_idx = _get_rbac_idx()
                _idx_dec = await _rbac_idx.check_access(
                    _RBACSubjectIdx(user_id=user_id),
                    "write", "domain_data", domain_id,
                    log_decision=False,
                )
                _has_write = _idx_dec.allowed
            if not _has_write and not await is_domain_admin(user_id, domain_id or ""):
                return {"error": "Permission denied: indexing requires write access to this domain."}

        # Verify domain exists
        d = await get_domain(domain_id)
        if not d:
            return {"error": f"Domain '{domain_id}' not found. Register it first via manage_domain."}

        if not content.strip():
            return {"error": "Document content is empty."}

        # Size guard — prevent cost explosion from huge documents
        _MAX_CONTENT_SIZE = 500_000  # 500KB max
        if len(content) > _MAX_CONTENT_SIZE:
            return {"error": f"Content too large ({len(content):,} bytes). Max {_MAX_CONTENT_SIZE:,} bytes."}

        # Index
        chunk_count = await index_document(
            domain_id=domain_id,
            file_path=file_path,
            content=content,
            strategy=strategy,
        )

        return {
            "success": True,
            "domain_id": domain_id,
            "file_path": file_path,
            "chunks_created": chunk_count,
            "strategy": strategy,
        }

    return await _run_with_timeout(_do(), "index_document", timeout=120)
