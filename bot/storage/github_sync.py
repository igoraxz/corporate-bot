# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""GitHub repository sync for knowledge domains.

Handles:
- Webhook-triggered incremental sync (push events)
- Polling-based sync (fallback when webhooks miss)
- Full repo clone/fetch for initial indexing
- Content retrieval via GitHub API (for individual files)
- Webhook signature verification (HMAC-SHA256)

Authentication:
- GitHub App (preferred): installation token for repo access
- Personal Access Token (simpler): single token for all repos
- Deploy key (SSH): for git clone operations

Architecture:
- Webhook arrives → identify domain → fetch changed files → index
- Polling: scheduled task checks each domain's repo for new commits
- Initial sync: clone entire repo, walk file tree, index all matching files
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# GitHub API base URL
_GITHUB_API = "https://api.github.com"

# Supported document extensions for indexing
_INDEXABLE_EXTENSIONS = frozenset({
    ".md", ".txt", ".rst",          # Text/docs
    ".py", ".js", ".ts", ".go",     # Code
    ".yaml", ".yml", ".json",       # Config
    ".sh", ".bash",                 # Scripts
    ".html", ".css",                # Web
    ".toml", ".cfg",                # Config (alt)
    ".csv", ".xml",                 # Data
})

# Max file size to index (skip binaries/large files)
_MAX_FILE_SIZE_BYTES = 500_000  # 500KB


def verify_webhook_signature(payload_body: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    Args:
        payload_body: Raw request body bytes
        signature: X-Hub-Signature-256 header value (sha256=...)
        secret: Webhook secret configured in GitHub

    Returns: True if signature is valid.
    """
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


class GitHubSyncClient:
    """GitHub API client for fetching repository content.

    Supports both GitHub App tokens and Personal Access Tokens.
    """

    def __init__(self, token: str = "", app_id: str = "", private_key_path: str = ""):
        """Initialize with either a PAT or GitHub App credentials.

        Args:
            token: Personal Access Token (if using PAT auth)
            app_id: GitHub App ID (if using App auth)
            private_key_path: Path to App private key PEM file
        """
        self._token = token
        self._app_id = app_id
        self._private_key_path = private_key_path
        self._http: httpx.AsyncClient | None = None
        self._installation_token: str = ""
        self._installation_token_expires: float = 0

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def _get_auth_token(self) -> str:
        """Get auth token (PAT or installation token)."""
        if self._token:
            return self._token
        # TODO: GitHub App JWT → installation token flow
        # For MVP, use PAT
        return self._token

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def get_file_content(self, repo: str, path: str, branch: str = "main") -> str | None:
        """Fetch a single file's content from GitHub.

        Args:
            repo: "owner/repo" format
            path: File path within repo
            branch: Branch/ref to fetch from

        Returns: File content as string, or None on failure.
        """
        token = await self._get_auth_token()
        http = await self._get_http()

        url = f"{_GITHUB_API}/repos/{repo}/contents/{path}"
        headers = {"Accept": "application/vnd.github.raw+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = await http.get(url, headers=headers, params={"ref": branch})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            log.error("GitHub get_file_content failed (%s/%s): %s", repo, path, e)
            return None

    async def list_files(self, repo: str, path: str = "", branch: str = "main") -> list[dict]:
        """List files in a repo directory (recursive via tree API).

        Returns list of {path, size, sha} dicts for files only (not dirs).
        """
        token = await self._get_auth_token()
        http = await self._get_http()

        # Use Git Trees API for recursive listing (single API call)
        url = f"{_GITHUB_API}/repos/{repo}/git/trees/{branch}"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = await http.get(url, headers=headers, params={"recursive": "1"})
            resp.raise_for_status()
            data = resp.json()

            files = []
            for item in data.get("tree", []):
                if item.get("type") != "blob":
                    continue
                file_path = item.get("path", "")
                # Filter by path prefix if specified
                if path and not file_path.startswith(path):
                    continue
                # Filter by extension
                ext = Path(file_path).suffix.lower()
                if ext not in _INDEXABLE_EXTENSIONS:
                    continue
                # Filter by size
                size = item.get("size", 0)
                if size > _MAX_FILE_SIZE_BYTES:
                    continue
                files.append({
                    "path": file_path,
                    "size": size,
                    "sha": item.get("sha", ""),
                })
            return files

        except Exception as e:
            log.error("GitHub list_files failed (%s): %s", repo, e)
            return []

    async def get_latest_commit(self, repo: str, branch: str = "main") -> str:
        """Get latest commit SHA for a branch."""
        token = await self._get_auth_token()
        http = await self._get_http()

        url = f"{_GITHUB_API}/repos/{repo}/commits/{branch}"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = await http.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("sha", "")[:12]
        except Exception as e:
            log.error("GitHub get_latest_commit failed (%s): %s", repo, e)
            return ""


# ============================================================
# SSH Deploy Key Support (CB-99)
# ============================================================

# Per-domain sync locks to prevent concurrent syncs
_domain_sync_locks: dict[str, asyncio.Lock] = {}


def _generate_ed25519_keypair() -> tuple[str, str]:
    """Generate Ed25519 SSH keypair. Returns (private_key_pem, public_key_openssh)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private_pem, public_openssh


def _write_temp_ssh_key(private_key_pem: str, domain_id: str) -> tuple[str, callable]:
    """Write SSH private key to tmpfs. Returns (path, cleanup_fn).

    Uses /dev/shm (memory-only filesystem) to avoid disk persistence.
    File created with O_CREAT|O_EXCL + mode 0600 to prevent races.
    """
    import secrets
    tmpdir = "/dev/shm"
    filename = f"deploy_key_{domain_id}_{secrets.token_hex(8)}"
    filepath = os.path.join(tmpdir, filename)
    fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, private_key_pem.encode())
    finally:
        os.close(fd)

    def cleanup():
        try:
            os.unlink(filepath)
        except OSError:
            pass

    return filepath, cleanup


async def git_clone_or_pull_ssh(
    repo_url: str,
    target_dir: str,
    branch: str,
    ssh_key_path: str,
    timeout: int = 120,
) -> bool:
    """Clone or pull a repo via SSH with deploy key.

    Returns True on success, False on failure.
    """
    # Minimal env for git/ssh — don't leak API keys/tokens to subprocess
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_SSH_COMMAND": f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }

    if os.path.isdir(os.path.join(target_dir, ".git")):
        # Pull existing clone
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", target_dir, "fetch", "origin", branch,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            log.error("git fetch failed for %s: %s", repo_url, stderr.decode()[:200])
            return False
        # Reset to fetched branch
        proc2 = await asyncio.create_subprocess_exec(
            "git", "-C", target_dir, "reset", "--hard", f"origin/{branch}",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc2.communicate(), timeout=30)
        return proc2.returncode == 0
    else:
        # Fresh clone (-- separates options from positional args for safety)
        os.makedirs(target_dir, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", "--branch", branch, "--",
            f"git@github.com:{repo_url}.git", target_dir,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            log.error("git clone failed for %s: %s", repo_url, stderr.decode()[:200])
            return False
        return True


async def sync_domain_ssh(domain_id: str, private_key_pem: str,
                          repo: str, branch: str = "main") -> dict:
    """Full sync via SSH git clone/pull.

    Clones repo to /app/data/domain_files/{domain_id}/, then walks
    the filesystem to index files (same output format as sync_domain_full).
    """
    from bot.storage.domains import (
        index_document, update_domain_status,
    )
    target_dir = f"/app/data/domain_files/{domain_id}"
    result = {"files_indexed": 0, "chunks_created": 0, "errors": []}

    # Acquire per-domain lock to prevent concurrent syncs
    lock = _domain_sync_locks.setdefault(domain_id, asyncio.Lock())
    async with lock:
        key_path, cleanup = _write_temp_ssh_key(private_key_pem, domain_id)
        try:
            await update_domain_status(domain_id, "syncing")
            success = await git_clone_or_pull_ssh(repo, target_dir, branch, key_path)
            if not success:
                result["errors"].append("git clone/pull failed")
                await update_domain_status(domain_id, "error", error="git clone/pull failed")
                return result

            # Walk the cloned repo and index files
            from pathlib import Path
            repo_path = Path(target_dir)
            for fpath in sorted(repo_path.rglob("*")):
                if fpath.is_dir() or fpath.name.startswith("."):
                    continue
                if fpath.suffix.lower() not in _INDEXABLE_EXTENSIONS:
                    continue
                rel_path = str(fpath.relative_to(repo_path))
                # Skip git internals and large files
                if rel_path.startswith(".git/") or fpath.stat().st_size > 500_000:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    chunks = await index_document(domain_id, rel_path, content)
                    result["files_indexed"] += 1
                    result["chunks_created"] += chunks
                except Exception as e:
                    result["errors"].append(f"{rel_path}: {str(e)[:100]}")

            # CB-101: Sync skills from domain repo /.claude/skills/
            _skills_synced = await _sync_domain_skills(domain_id, repo_path)
            if _skills_synced:
                result["skills_synced"] = _skills_synced

            # CB-101: Sync prompt modules from domain repo /prompts/ or /modules/
            _modules_synced = await _sync_domain_modules(domain_id, repo_path)
            if _modules_synced:
                result["modules_synced"] = _modules_synced

            await update_domain_status(domain_id, "active")
            log.info("SSH sync complete for %s: %d files, %d chunks, %d skills, %d modules",
                     domain_id, result["files_indexed"], result["chunks_created"],
                     len(_skills_synced), len(_modules_synced))
        finally:
            cleanup()

    return result


# Module-level sync client
async def _sync_domain_skills(domain_id: str, repo_path: "Path") -> list[str]:
    """Sync skills from domain repo /.claude/skills/ to local skills dir (CB-101).

    Skills are prefixed with domain_id to avoid collisions with core skills.
    Returns list of synced skill names.
    """
    import shutil
    from pathlib import Path
    skills_src = repo_path / ".claude" / "skills"
    if not skills_src.is_dir():
        return []

    skills_dst = Path("/app/.claude/skills")
    synced = []
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        # Security: block symlinks and path traversal from untrusted repos
        if skill_dir.is_symlink():
            log.warning("Skipping symlink in domain skills: %s", skill_dir)
            continue
        if "/" in skill_dir.name or ".." in skill_dir.name:
            log.warning("Skipping suspicious skill dir name: %s", skill_dir.name)
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            continue
        # Prefix with domain_id to avoid collisions
        target_name = f"{domain_id}-{skill_dir.name}"
        target_dir = skills_dst / target_name
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(skill_md), str(target_dir / "SKILL.md"))
        synced.append(target_name)
        log.debug("Synced domain skill: %s → %s", skill_dir.name, target_name)

    if synced:
        log.info("Domain %s: synced %d skills: %s", domain_id, len(synced), synced)
    return synced


async def _sync_domain_modules(domain_id: str, repo_path: "Path") -> list[str]:
    """Sync prompt modules from domain repo /prompts/ or /modules/ (CB-101).

    Modules are prefixed with domain_id and placed in claude_md_modules dir.
    Returns list of synced module names.
    """
    import shutil
    from pathlib import Path

    # Check both /prompts/ and /modules/ directories
    modules_src = None
    for dirname in ("prompts", "modules"):
        candidate = repo_path / dirname
        if candidate.is_dir():
            modules_src = candidate
            break
    if modules_src is None:
        return []

    modules_dst = Path("/app/data/claude_md_modules")
    modules_dst.mkdir(parents=True, exist_ok=True)
    synced = []
    for mod_file in sorted(modules_src.glob("*.md")):
        if mod_file.name.startswith("."):
            continue
        # Security: block symlinks from untrusted repos
        if mod_file.is_symlink():
            log.warning("Skipping symlink in domain modules: %s", mod_file)
            continue
        target_name = f"{domain_id}-{mod_file.stem}"
        target_path = modules_dst / f"{target_name}.md"
        shutil.copy2(str(mod_file), str(target_path))
        synced.append(target_name)
        log.debug("Synced domain module: %s → %s", mod_file.name, target_name)

    if synced:
        log.info("Domain %s: synced %d modules: %s", domain_id, len(synced), synced)
    return synced


_sync_client: GitHubSyncClient | None = None


def init_sync_client(token: str = "", app_id: str = "", private_key_path: str = "") -> GitHubSyncClient:
    """Initialize the global sync client."""
    global _sync_client
    _sync_client = GitHubSyncClient(token=token, app_id=app_id, private_key_path=private_key_path)
    return _sync_client


def get_sync_client() -> GitHubSyncClient | None:
    return _sync_client


async def _get_deploy_key(domain_id: str) -> str | None:
    """Get SSH deploy key for a domain. Checks credential_store then file fallback.

    CB-131: Deploy keys enable SSH-based sync (least-privilege, per-repo scope).
    Returns private key PEM string, or None if no key available.
    """
    # 1. Try credential_store (encrypted, preferred)
    try:
        from bot.auth.credentials import get_credential
        cred = await get_credential(f"ssh:deploy-key-{domain_id}", decrypt=True)
        if cred:
            key = (cred.get("data") or {}).get("private_key", "")
            if key:
                log.debug("Deploy key for %s: found in credential_store", domain_id)
                return key
    except Exception as e:
        log.debug("Deploy key credential_store lookup failed for %s: %s", domain_id, e)

    # 2. Fallback to file-based key (legacy / bootstrap)
    from pathlib import Path
    key_file = Path(f"/app/data/deploy-keys/{domain_id}.pem")
    if key_file.exists():
        # Security: reject symlinks and overly permissive files
        if key_file.is_symlink():
            log.warning("Deploy key file is a symlink, skipping: %s", key_file)
            return None
        _mode = key_file.stat().st_mode & 0o777
        if _mode & 0o077:
            log.warning("Deploy key file has permissive mode %o, skipping: %s", _mode, key_file)
            return None
        try:
            key = key_file.read_text()
            if key.strip():
                # Ensure trailing newline — SSH keys require it
                if not key.endswith("\n"):
                    key += "\n"
                log.debug("Deploy key for %s: found at %s", domain_id, key_file)
                return key
        except Exception as e:
            log.warning("Deploy key file read failed for %s: %s", domain_id, e)

    return None


async def sync_domain_auto(domain_id: str, repo: str, branch: str = "main") -> dict:
    """Sync a GitHub domain using the best available method.

    CB-131: Prefers SSH deploy keys (per-repo, least privilege) over REST API
    (shared token, broad scope). Falls back to REST API if no deploy key.
    """
    import re as _re_sync
    # Validate repo format (owner/name) to prevent injection
    if not repo or not _re_sync.match(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$', repo):
        return {"files_indexed": 0, "chunks_created": 0,
                "errors": [f"Invalid repo format: {repo!r}. Expected 'owner/name'."]}
    # Validate branch (reject option-like values)
    if branch.startswith("-"):
        return {"files_indexed": 0, "chunks_created": 0,
                "errors": [f"Invalid branch: {branch!r}"]}

    # Try SSH deploy key first
    deploy_key = await _get_deploy_key(domain_id)
    if deploy_key:
        log.info("Syncing %s via SSH deploy key", domain_id)
        return await sync_domain_ssh(domain_id, deploy_key, repo, branch)

    # Fallback to REST API
    client = get_sync_client()
    if client:
        log.info("Syncing %s via REST API (no deploy key found)", domain_id)
        return await sync_domain_full(domain_id)

    return {"files_indexed": 0, "chunks_created": 0,
            "errors": [f"No sync method available for {domain_id}: "
                       "no deploy key and no REST API token configured"]}


# ============================================================
# Sync Operations
# ============================================================

async def sync_domain_full(domain_id: str) -> dict:
    """Full sync of a domain — fetch all files and index.

    Used for initial indexing or forced re-index.

    Returns: {"files_indexed": N, "chunks_created": N, "errors": [...]}
    """
    from bot.storage.domains import (
        get_domain, index_document, content_hash,
        get_document_hash,
    )
    import yaml

    domain = await get_domain(domain_id)
    if not domain:
        return {"error": f"Domain '{domain_id}' not found"}

    config = yaml.safe_load(domain.get("config_yaml", "{}"))
    domain_cfg = config.get("domain", config)
    source = domain_cfg.get("source", {})
    indexing = domain_cfg.get("indexing", {})

    repo = source.get("repo", "")
    branch = source.get("branch", "main")
    paths = source.get("paths", ["."])
    exclude = source.get("exclude", [])
    strategy = indexing.get("chunking_strategy", "markdown_headers")
    chunk_size = indexing.get("chunk_size", 1500)

    if not repo:
        return {"error": "Domain has no source repo configured"}

    client = get_sync_client()
    if not client:
        return {"error": "GitHub sync client not initialized (missing token)"}

    # Update status to syncing
    from bot.storage.db import open_db
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    async with open_db() as db:
        await db.execute(
            "UPDATE domain_registry SET status='syncing', updated_at=? WHERE domain_id=?",
            (now, domain_id),
        )
        await db.commit()

    files_indexed = 0
    chunks_created = 0
    errors: list[str] = []

    # List all files in configured paths
    all_files: list[dict] = []
    for path_prefix in paths:
        files = await client.list_files(repo, path_prefix.rstrip("/"), branch)
        all_files.extend(files)

    # Filter exclusions
    import fnmatch
    filtered_files = []
    for f in all_files:
        excluded = False
        for pattern in exclude:
            if fnmatch.fnmatch(f["path"], pattern):
                excluded = True
                break
        if not excluded:
            filtered_files.append(f)

    log.info("Domain %s full sync: %d files to process (from %d total)",
             domain_id, len(filtered_files), len(all_files))

    # Process files (with concurrency limit)
    sem = asyncio.Semaphore(5)  # max 5 concurrent GitHub API calls

    async def _process_file(file_info: dict) -> tuple[int, str]:
        async with sem:
            file_path = file_info["path"]
            try:
                content = await client.get_file_content(repo, file_path, branch)
                if content is None:
                    return 0, f"Failed to fetch {file_path}"

                # Check if content changed (skip if hash matches)
                new_hash = content_hash(content)
                existing_hash = await get_document_hash(domain_id, file_path)
                if existing_hash == new_hash:
                    return 0, ""  # unchanged, skip

                # Parse document content (H7 fix: use doc_parser for PDF/DOCX/code)
                from bot.storage.doc_parser import is_supported, parse_file_content
                if is_supported(file_path):
                    parsed_content, auto_strategy = parse_file_content(file_path, content)
                else:
                    parsed_content = content
                    auto_strategy = strategy

                # Index the document
                chunks = await index_document(
                    domain_id=domain_id,
                    file_path=file_path,
                    content=parsed_content,
                    strategy=auto_strategy,
                    chunk_size=chunk_size,
                )
                return chunks, ""

            except Exception as e:
                return 0, f"Error indexing {file_path}: {e}"

    tasks = [_process_file(f) for f in filtered_files]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result))
        elif isinstance(result, tuple):
            chunks, error = result
            if chunks > 0:
                files_indexed += 1
                chunks_created += chunks
            if error:
                errors.append(error)

    # Update domain status
    status = "error" if errors and not files_indexed else "active"
    error_msg = "; ".join(errors[:3]) if errors else ""
    async with open_db() as db:
        await db.execute("""
            UPDATE domain_registry
            SET status=?, last_sync_at=?, error_message=?, updated_at=?
            WHERE domain_id=?
        """, (status, now, error_msg, now, domain_id))
        await db.commit()

    log.info("Domain %s sync complete: %d files, %d chunks, %d errors",
             domain_id, files_indexed, chunks_created, len(errors))

    return {
        "domain_id": domain_id,
        "files_indexed": files_indexed,
        "chunks_created": chunks_created,
        "files_skipped_unchanged": len(filtered_files) - files_indexed - len(errors),
        "errors": errors[:5],
    }


async def sync_domain_incremental(domain_id: str, changed_files: list[str], removed_files: list[str]) -> dict:
    """Incremental sync — process only changed/removed files.

    Called by webhook handler after push event parsing.
    """
    from bot.storage.domains import (
        get_domain, index_document, remove_document,
    )
    import yaml

    domain = await get_domain(domain_id)
    if not domain:
        return {"error": f"Domain '{domain_id}' not found"}

    config = yaml.safe_load(domain.get("config_yaml", "{}"))
    domain_cfg = config.get("domain", config)
    source = domain_cfg.get("source", {})
    indexing = domain_cfg.get("indexing", {})

    repo = source.get("repo", "")
    branch = source.get("branch", "main")
    strategy = indexing.get("chunking_strategy", "markdown_headers")
    chunk_size = indexing.get("chunk_size", 1500)

    client = get_sync_client()
    if not client:
        return {"error": "GitHub sync client not initialized"}

    files_indexed = 0
    chunks_created = 0
    files_removed = 0

    # Remove deleted files
    for file_path in removed_files:
        count = await remove_document(domain_id, file_path)
        if count > 0:
            files_removed += 1

    # Index changed files
    for file_path in changed_files:
        # Filter by extension
        ext = Path(file_path).suffix.lower()
        if ext not in _INDEXABLE_EXTENSIONS:
            continue

        content = await client.get_file_content(repo, file_path, branch)
        if content is None:
            continue

        chunks = await index_document(
            domain_id=domain_id,
            file_path=file_path,
            content=content,
            strategy=strategy,
            chunk_size=chunk_size,
        )
        if chunks > 0:
            files_indexed += 1
            chunks_created += chunks

    # Update domain timestamp
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    from bot.storage.db import open_db
    async with open_db() as db:
        await db.execute(
            "UPDATE domain_registry SET last_sync_at=?, updated_at=? WHERE domain_id=?",
            (now, now, domain_id),
        )
        await db.commit()

    return {
        "domain_id": domain_id,
        "files_indexed": files_indexed,
        "chunks_created": chunks_created,
        "files_removed": files_removed,
    }


# ============================================================
# Polling-based Sync (scheduled task fallback)
# ============================================================

async def poll_all_domains() -> dict:
    """Poll all active domains for changes. Called by scheduler.

    Routes to the appropriate sync backend based on source type:
    - github: checks latest commit, triggers full sync if stale
    - graph_api: triggers Graph API sync (delta query support)

    Returns summary of sync operations.
    """
    from bot.storage.domains import list_domains
    import yaml

    domains = await list_domains()
    active = [d for d in domains if d["status"] == "active"]

    if not active:
        return {"polled": 0, "synced": 0}

    synced = 0
    errors: list[str] = []

    for domain in active:
        domain_id = domain["domain_id"]
        config = yaml.safe_load(domain.get("config_yaml", "{}"))
        domain_cfg = config.get("domain", config)
        source = domain_cfg.get("source", {})
        indexing_cfg = domain_cfg.get("indexing", {})
        source_type = source.get("type", "github")
        poll_interval = indexing_cfg.get("poll_interval_minutes", 5)

        # Check if enough time has passed since last sync
        from bot.storage.db import open_db
        async with open_db() as db:
            cursor = await db.execute(
                "SELECT last_sync_at FROM domain_registry WHERE domain_id = ?",
                (domain_id,),
            )
            row = await cursor.fetchone()

        last_sync = row["last_sync_at"] if row else None

        needs_sync = False
        if not last_sync:
            needs_sync = True
        else:
            import datetime
            try:
                last_dt = datetime.datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                age_minutes = (datetime.datetime.now(datetime.timezone.utc) - last_dt).total_seconds() / 60
                if age_minutes >= poll_interval:
                    needs_sync = True
            except (ValueError, AttributeError):
                needs_sync = True

        if not needs_sync:
            continue

        # Route to appropriate sync backend
        try:
            if source_type == "graph_api":
                from bot.storage.graph_sync import sync_graph_domain
                result = await sync_graph_domain(domain_id)
                if result.get("indexed", 0) > 0:
                    synced += 1
                if result.get("errors", 0) > 0:
                    errors.append(f"{domain_id}: {result.get('errors')} files failed")
                if result.get("error"):
                    errors.append(f"{domain_id}: {result['error']}")
            elif source_type == "github":
                repo = source.get("repo", "")
                if not repo:
                    continue
                # CB-131: Use auto-routing (SSH deploy key preferred, REST API fallback)
                result = await sync_domain_auto(domain_id, repo,
                                                source.get("branch", "main"))
                if result.get("files_indexed", 0) > 0:
                    synced += 1
                if result.get("errors"):
                    errors.extend(result["errors"][:2])
            else:
                log.debug("Skipping domain %s with unsupported source type: %s",
                          domain_id, source_type)
        except Exception as e:
            errors.append(f"{domain_id}: {e}")
            log.error("Poll sync failed for %s: %s", domain_id, e)

    return {"polled": len(active), "synced": synced, "errors": errors[:5] if errors else []}
