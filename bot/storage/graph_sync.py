# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Microsoft Graph API document sync for knowledge domains.

Indexes documents from SharePoint sites, Teams channel files, and OneDrive
for Business into the knowledge domain RAG system. Parallel to github_sync.py.

Requires Azure AD permissions: Files.Read.All, Sites.Read.All
Token obtained from M365 MCP auth cache or delegated flow.

Architecture:
- List files via Graph API drive endpoints
- Filter by extension (reuse doc_parser supported formats)
- Download content, parse via Gemini-powered doc_parser
- Index into RAG via existing index_document pipeline
- Delta query support for incremental sync
"""

import asyncio
import logging
import time
from pathlib import Path

import httpx

from bot.storage.db import open_db

log = logging.getLogger(__name__)

# File size limit for indexing
_MAX_FILE_SIZE_BYTES = 10_000_000  # 10MB (Graph API files can be large)

# Concurrent download semaphore
_DOWNLOAD_SEM = asyncio.Semaphore(5)


class GraphSyncClient:
    """Microsoft Graph API client for document sync.

    Fetches files from SharePoint/OneDrive drives and indexes them
    into knowledge domains via the existing doc_parser + index_document pipeline.
    """

    def __init__(self, access_token: str):
        """Initialize with a Graph API access token.

        Token must have Files.Read.All and Sites.Read.All scopes.
        Obtained from M365 MCP auth cache or delegated flow.
        """
        self.token = access_token
        self.base_url = "https://graph.microsoft.com/v1.0"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30.0,
            )
        return self._client

    def _is_safe_graph_url(self, url: str) -> bool:
        """Validate that a URL points to the Microsoft Graph API (SSRF prevention)."""
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            return (
                parsed.scheme == "https"
                and parsed.hostname == "graph.microsoft.com"
            )
        except Exception:
            return False

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def list_drive_files(
        self,
        drive_id: str,
        folder_path: str = "",
        recursive: bool = True,
    ) -> list[dict]:
        """List files in a SharePoint/OneDrive drive folder.

        Returns list of {name, path, size, last_modified,
        item_id, mime_type, content_hash}.
        """
        client = await self._get_client()

        # Build endpoint
        if folder_path:
            endpoint = f"/drives/{drive_id}/root:/{folder_path}:/children"
        else:
            endpoint = f"/drives/{drive_id}/root/children"

        all_files: list[dict] = []
        folders_to_process: list[tuple[str, str]] = [(endpoint, folder_path)]

        while folders_to_process:
            ep, current_path = folders_to_process.pop(0)

            try:
                resp = await client.get(ep, params={"$top": "200"})
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning("Graph API list failed for %s: %s", ep, e)
                continue

            for item in data.get("value", []):
                item_path = f"{current_path}/{item['name']}" if current_path else item["name"]

                if "folder" in item and recursive:
                    # Queue subfolder for processing
                    child_ep = f"/drives/{drive_id}/items/{item['id']}/children"
                    folders_to_process.append((child_ep, item_path))
                elif "file" in item:
                    size = item.get("size", 0)
                    if size > _MAX_FILE_SIZE_BYTES:
                        log.debug("Skipping oversized file %s (%d bytes)", item_path, size)
                        continue

                    # Check if format is supported
                    from bot.storage.doc_parser import is_supported
                    if not is_supported(item["name"]):
                        continue

                    all_files.append({
                        "name": item["name"],
                        "path": item_path,
                        "size": size,
                        "last_modified": item.get("lastModifiedDateTime", ""),
                        # Note: @microsoft.graph.downloadUrl intentionally NOT stored (pre-auth URL = SSRF risk)
                        "item_id": item["id"],
                        "mime_type": item.get("file", {}).get("mimeType", ""),
                        "content_hash": item.get("file", {}).get("hashes", {}).get("sha256Hash", ""),
                    })

            # Handle pagination — validate URL to prevent SSRF
            next_link = data.get("@odata.nextLink")
            if next_link:
                if self._is_safe_graph_url(next_link):
                    folders_to_process.insert(0, (next_link.replace(self.base_url, ""), current_path))
                else:
                    log.warning("Rejecting unexpected pagination URL: %s", next_link[:100])

        log.info("Listed %d indexable files from drive %s", len(all_files), drive_id)
        return all_files

    async def download_file(self, drive_id: str, item_id: str) -> bytes:
        """Download file content from Graph API."""
        async with _DOWNLOAD_SEM:
            client = await self._get_client()
            resp = await client.get(f"/drives/{drive_id}/items/{item_id}/content")
            resp.raise_for_status()
            return resp.content

    async def get_site_drive_id(self, site_id: str) -> str | None:
        """Get the default document library drive ID for a SharePoint site."""
        client = await self._get_client()
        try:
            resp = await client.get(f"/sites/{site_id}/drive")
            resp.raise_for_status()
            return resp.json().get("id")
        except Exception as e:
            log.error("Failed to get drive for site %s: %s", site_id, e)
            return None

    async def get_channel_drive_id(self, team_id: str, channel_id: str) -> str | None:
        """Get the drive ID for a Teams channel's file folder."""
        client = await self._get_client()
        try:
            resp = await client.get(f"/teams/{team_id}/channels/{channel_id}/filesFolder")
            resp.raise_for_status()
            data = resp.json()
            return data.get("parentReference", {}).get("driveId")
        except Exception as e:
            log.error("Failed to get drive for channel %s/%s: %s", team_id, channel_id, e)
            return None

    async def get_delta(self, drive_id: str, delta_link: str = "") -> tuple[list[dict], str]:
        """Get changed files since last sync using delta query.

        Args:
            drive_id: Drive to query
            delta_link: Previous deltaLink (empty for initial sync)

        Returns: (changed_items, new_delta_link)
        """
        client = await self._get_client()

        if delta_link:
            if not self._is_safe_graph_url(delta_link):
                log.warning("Rejecting unsafe delta_link: %s", delta_link[:100])
                delta_link = ""
            else:
                endpoint = delta_link.replace(self.base_url, "")
        if not delta_link:
            endpoint = f"/drives/{drive_id}/root/delta"

        all_changes: list[dict] = []
        while endpoint:
            try:
                resp = await client.get(endpoint, params={"$top": "200"})
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning("Delta query failed: %s", type(e).__name__)
                return all_changes, delta_link  # Return what we have

            for item in data.get("value", []):
                all_changes.append({
                    "item_id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "path": item.get("parentReference", {}).get("path", ""),
                    "deleted": "deleted" in item,
                    "is_file": "file" in item,
                    "size": item.get("size", 0),
                    "last_modified": item.get("lastModifiedDateTime", ""),
                })

            # Validate pagination URL (SSRF prevention)
            raw_next = data.get("@odata.nextLink", "")
            if raw_next and self._is_safe_graph_url(raw_next):
                endpoint = raw_next.replace(self.base_url, "")
            else:
                endpoint = ""
                if raw_next:
                    log.warning("Rejecting unsafe nextLink in delta: %s", raw_next[:100])
            if not endpoint:
                # Final page — get the deltaLink for next sync
                new_delta = data.get("@odata.deltaLink", "")
                return all_changes, new_delta

        return all_changes, delta_link


# Module-level singleton
_sync_client: GraphSyncClient | None = None


def init_graph_sync_client(access_token: str) -> GraphSyncClient:
    """Initialize the Graph sync client singleton."""
    global _sync_client
    _sync_client = GraphSyncClient(access_token)
    return _sync_client


def get_graph_sync_client() -> GraphSyncClient | None:
    """Get the initialized Graph sync client."""
    return _sync_client


async def _get_graph_token() -> str | None:
    """Get a Graph API access token from M365 MCP auth cache.

    Reads from the Softeria MSAL cache used by the M365 MCP server.
    """
    import json

    # Try both cache paths (known dual-path issue)
    for cache_path in [
        Path("/app/data/ms365-mcp/msal_cache.json"),
        Path("/app/data/credentials/ms365-mcp/msal_cache.json"),
    ]:
        if not cache_path.exists():
            continue
        try:
            raw = cache_path.read_text()
            envelope = json.loads(raw)
            # Softeria envelope format
            cache_data = envelope.get("data", raw)
            if isinstance(cache_data, str):
                cache_data = json.loads(cache_data)

            # Extract access token from MSAL cache (with expiry + scope checks)
            import time as _time
            now_epoch = int(_time.time())
            access_tokens = cache_data.get("AccessToken", {})
            for _key, token_entry in access_tokens.items():
                # Check expiry
                expires_on = int(token_entry.get("expires_on", 0))
                if expires_on and expires_on <= now_epoch:
                    continue  # Skip expired tokens
                # Require Files or Sites scope
                target = token_entry.get("target", "")
                if "files" in target.lower() or "sites" in target.lower():
                    return token_entry.get("secret", "")
            # Do NOT fall back to tokens without Files/Sites scope
        except Exception as e:
            log.debug("Failed to read M365 cache at %s: %s", cache_path, e)

    log.warning("No Graph API token found in M365 cache — SharePoint sync unavailable")
    return None


async def sync_graph_domain(domain_id: str) -> dict:
    """Full sync of a Graph API-sourced domain.

    1. Get domain config (site_id/drive_id/team_id/channel_id)
    2. Resolve drive ID
    3. List all files
    4. Download + parse + index changed files
    5. Remove deleted files
    6. Update domain status

    Returns: {indexed: N, skipped: N, removed: N, errors: N, total_files: N}
    """
    import fnmatch
    import yaml
    from bot.storage.domains import (
        get_domain, get_document_hash, index_document,
        update_document_record,
        content_hash,
    )
    from bot.storage.doc_parser import parse_file_content_async

    domain = await get_domain(domain_id)
    if not domain:
        return {"error": f"Domain {domain_id} not found"}

    # Parse config
    try:
        cfg = yaml.safe_load(domain["config_yaml"])
        d = cfg.get("domain", cfg)
        source = d.get("source", {})
        indexing_cfg = d.get("indexing", {})
    except Exception as e:
        return {"error": f"Failed to parse domain config: {e}"}

    if source.get("type") != "graph_api":
        return {"error": f"Domain {domain_id} source is '{source.get('type')}', not graph_api"}

    # Get access token
    token = await _get_graph_token()
    if not token:
        return {"error": "No Graph API token available — configure M365 auth with Files.Read.All scope"}

    client = GraphSyncClient(token)

    try:
        # Resolve drive ID
        drive_id = source.get("drive_id", "")
        if not drive_id:
            site_id = source.get("site_id", "")
            team_id = source.get("team_id", "")
            channel_id = source.get("channel_id", "")

            if site_id:
                drive_id = await client.get_site_drive_id(site_id)
            elif team_id and channel_id:
                drive_id = await client.get_channel_drive_id(team_id, channel_id)

            if not drive_id:
                return {"error": "Could not resolve drive ID from domain config"}

        # List files
        paths = source.get("paths", [""])
        exclude = source.get("exclude", [])
        all_files: list[dict] = []
        for folder_path in paths:
            files = await client.list_drive_files(drive_id, folder_path)
            all_files.extend(files)

        # Apply exclusions
        if exclude:
            all_files = [
                f for f in all_files
                if not any(fnmatch.fnmatch(f["path"], pat) for pat in exclude)
            ]

        # Mark domain as syncing
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with open_db() as db:
            await db.execute(
                "UPDATE domain_registry SET status='syncing', updated_at=? WHERE domain_id=?",
                (now, domain_id),
            )
            await db.commit()

        stats = {"indexed": 0, "skipped": 0, "removed": 0, "errors": 0, "total_files": len(all_files)}

        chunk_size = indexing_cfg.get("chunk_size", 1500)

        # Process files concurrently — returns status string to avoid shared dict race
        async def _process_file(file_info: dict) -> str:
            """Returns 'indexed', 'skipped', or 'error'."""
            file_path = file_info["path"]
            try:
                # Check content hash for skip
                graph_hash = file_info.get("content_hash", "")
                stored_hash = await get_document_hash(domain_id, file_path)

                if stored_hash and graph_hash and stored_hash == graph_hash:
                    return "skipped"

                # Download
                raw_content = await client.download_file(drive_id, file_info["item_id"])

                # Hash for change detection (use our own if Graph didn't provide one)
                file_hash = graph_hash or content_hash(raw_content.decode("utf-8", errors="replace"))
                if stored_hash == file_hash:
                    return "skipped"

                # Parse via Gemini-powered doc parser
                parsed_text, strategy = await parse_file_content_async(file_path, raw_content)

                if not parsed_text or parsed_text.startswith("[PDF") or parsed_text.startswith("[DOCX") or parsed_text.startswith("[Image"):
                    log.warning("Empty/failed parse for %s/%s", domain_id, file_path)
                    return "error"

                # Index into RAG
                chunk_count = await index_document(
                    domain_id=domain_id,
                    file_path=file_path,
                    content=parsed_text,
                    strategy=strategy,
                    chunk_size=chunk_size,
                )

                # Update document record with hash
                await update_document_record(domain_id, file_path, file_hash, chunk_count)
                log.info("Indexed %s/%s: %d chunks", domain_id, file_path, chunk_count)
                return "indexed"

            except Exception as e:
                log.warning("Failed to process %s/%s: %s", domain_id, file_path, type(e).__name__)
                return "error"

        # Deduplicate files by item_id (overlapping paths may list same file twice)
        seen_ids: set[str] = set()
        unique_files = []
        for f in all_files:
            fid = f.get("item_id", f["path"])
            if fid not in seen_ids:
                seen_ids.add(fid)
                unique_files.append(f)
        if len(unique_files) < len(all_files):
            log.info("Deduped %d → %d files for %s", len(all_files), len(unique_files), domain_id)
        stats["total_files"] = len(unique_files)

        # Process all files, aggregate results (no shared mutable state)
        tasks = [_process_file(f) for f in unique_files]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.warning("Unhandled exception in _process_file: %s", type(r).__name__)
                stats["errors"] += 1
            elif isinstance(r, str):
                stats[r] = stats.get(r, 0) + 1

        # Update domain status
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with open_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM rag_chunks WHERE domain_id = ?",
                (domain_id,),
            )
            row = await cursor.fetchone()
            total_chunks = row["cnt"] if row else 0

            status = "active"
            error_msg = f"{stats['errors']} files failed" if stats["errors"] > 0 else ""

            await db.execute(
                "UPDATE domain_registry SET status=?, chunk_count=?, last_sync_at=?, "
                "error_message=?, updated_at=? WHERE domain_id=?",
                (status, total_chunks, now, error_msg, now, domain_id),
            )
            await db.commit()

        log.info("Graph sync complete for %s: %s", domain_id, stats)
        return stats

    finally:
        await client.close()
