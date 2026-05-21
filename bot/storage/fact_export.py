# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Bidirectional domain fact export — bot → GitHub (CB-100).

Exports non-sensitive domain facts to the domain's GitHub repo as YAML files.
Sensitivity gate: excludes credentials, PII patterns, media refs.
Pushes via deploy key (SSH) or GitHub API (PAT).

Usage:
  await export_domain_facts("teamwork")  # exports to repo /facts/
"""
import json
import logging
import re
import time

import yaml

log = logging.getLogger(__name__)

# Sensitivity gate patterns — facts matching these are NEVER exported
_SENSITIVE_CATEGORIES = frozenset({"credentials", "secrets"})
_PII_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),  # email
    re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),  # phone (US)
    re.compile(r"\b\+\d{1,3}\s?\d{4,14}\b"),  # phone (intl)
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b[A-Z]{2}\d{6,9}\b"),  # passport-like
]
_SECRET_KEYWORDS = frozenset({
    "password", "api_key", "api-key", "secret", "token",
    "private_key", "ssh_key", "bearer", "authorization",
    "aws_secret", "access_key", "client_secret",
})
_SECRET_VALUE_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (RSA |EC |ED25519 |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub PAT
    re.compile(r"sk-[A-Za-z0-9]{32,}"),   # OpenAI/Anthropic key
    re.compile(r"GOCSPX-[A-Za-z0-9_-]+"),  # Google OAuth secret
    # CB-112: Additional patterns from adversarial review
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),  # JWT token
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key ID
    re.compile(r"(postgresql|mysql|mongodb|redis)://\S+"),  # DB connection strings
]


def _is_high_entropy(value: str, threshold: float = 4.5, min_length: int = 20) -> bool:
    """Detect high-entropy strings that may be encoded secrets (CB-112).

    Shannon entropy > threshold + length > min_length → likely a secret.
    Normal English text: ~3.5-4.0 bits/char. Base64/hex secrets: ~5.0-6.0.
    """
    import math
    if len(value) < min_length:
        return False
    freq: dict[str, int] = {}
    for ch in value:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(value)
    entropy = -sum((c / length) * math.log2(c / length) for c in freq.values())
    return entropy > threshold


def _is_sensitive_fact(key: str, value: str, category: str,
                       doc_pointer: str = "") -> bool:
    """Check if a fact should be excluded from export."""
    # Category gate
    if category.lower() in _SENSITIVE_CATEGORIES:
        return True
    # Doc pointer = media reference → exclude
    if doc_pointer:
        return True
    # Key contains secret keywords
    key_lower = key.lower()
    for kw in _SECRET_KEYWORDS:
        if kw in key_lower:
            return True
    # Value contains PII patterns
    for pattern in _PII_PATTERNS:
        if pattern.search(value):
            return True
    # Value contains secret patterns (tokens, keys)
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return True
    # High-entropy detection: base64/hex encoded secrets (CB-112)
    # Split value into words and check each for high entropy
    for word in value.split():
        if _is_high_entropy(word):
            return True
    return False


async def export_domain_facts(domain_id: str) -> dict:
    """Export non-sensitive domain facts to structured YAML.

    Returns: {"exported": N, "filtered": N, "yaml_content": str}
    """
    from bot.storage.db import open_db

    async with open_db() as db:
        cursor = await db.execute(
            "SELECT fact_key, fact_value, category, subject, doc_pointer "
            "FROM message_facts WHERE expired = 0 AND domain_id = ? "
            "ORDER BY subject, category, fact_key",
            (domain_id,),
        )
        rows = await cursor.fetchall()

    if not rows:
        return {"exported": 0, "filtered": 0, "yaml_content": ""}

    # Group by subject → category → facts
    exported = {}
    filtered_count = 0
    for key, value, category, subject, doc_pointer in rows:
        if _is_sensitive_fact(key, value, category or "", doc_pointer or ""):
            filtered_count += 1
            continue
        subject_key = subject or "_general"
        cat_key = category or "general"
        exported.setdefault(subject_key, {}).setdefault(cat_key, {})[key] = value

    if not exported:
        return {"exported": 0, "filtered": filtered_count, "yaml_content": ""}

    # Format as YAML with metadata header
    output = {
        "_metadata": {
            "domain_id": domain_id,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_facts": len(rows),
            "exported_facts": sum(
                len(facts) for cats in exported.values() for facts in cats.values()
            ),
            "filtered_sensitive": filtered_count,
        },
        "facts": exported,
    }
    yaml_content = yaml.dump(output, default_flow_style=False, allow_unicode=True,
                              sort_keys=False, width=120)

    return {
        "exported": output["_metadata"]["exported_facts"],
        "filtered": filtered_count,
        "yaml_content": yaml_content,
    }


async def push_facts_to_github(domain_id: str, yaml_content: str,
                                repo: str, branch: str = "main") -> bool:
    """Push exported facts YAML to GitHub repo /facts/ directory.

    Uses deploy key (SSH) if available, else GitHub API (PAT).
    Returns True on success.
    """
    # Try deploy key path first
    try:
        from bot.auth.credentials import get_credential
        _cred = await get_credential(f"ssh:deploy-key-{domain_id}", decrypt=True)
        if _cred and _cred.get("data"):
            _data = json.loads(_cred["data"]) if isinstance(_cred["data"], str) else _cred["data"]
            private_key = _data.get("private_key", "")
            if private_key:
                return await _push_via_ssh(domain_id, yaml_content, repo, branch, private_key)
    except Exception:
        pass

    # Fallback: GitHub API (PAT)
    try:
        import config as _cfg
        _gh_token = getattr(_cfg, "GITHUB_TOKEN", "")
        if _gh_token:
            return await _push_via_api(yaml_content, repo, branch, _gh_token, domain_id)
    except Exception as e:
        log.error("Failed to push facts to GitHub for %s: %s", domain_id, e)

    return False


async def _push_via_ssh(domain_id: str, content: str, repo: str,
                        branch: str, private_key: str) -> bool:
    """Push via git commit using SSH deploy key."""
    import asyncio
    import os
    from bot.storage.github_sync import _write_temp_ssh_key

    target_dir = f"/app/data/domain_files/{domain_id}"
    facts_path = os.path.join(target_dir, "facts", "domain_facts.yaml")

    key_path, cleanup = _write_temp_ssh_key(private_key, domain_id)
    try:
        # Minimal env — don't leak API keys/tokens to git subprocess
        env = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_SSH_COMMAND": f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }

        # Ensure facts directory exists
        os.makedirs(os.path.dirname(facts_path), exist_ok=True)

        # Write the facts file
        with open(facts_path, "w", encoding="utf-8") as f:
            f.write(content)

        # Git add + commit + push
        for cmd in [
            ["git", "-C", target_dir, "add", "facts/domain_facts.yaml"],
            ["git", "-C", target_dir, "commit", "-m",
             f"bot: export domain facts ({domain_id})"],
            ["git", "-C", target_dir, "push", "origin", branch],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                # "nothing to commit" is not an error
                if b"nothing to commit" in stderr:
                    log.debug("No fact changes to push for %s", domain_id)
                    return True
                log.warning("Git command failed for fact export %s: %s",
                            domain_id, stderr.decode()[:200])
                return False

        log.info("Facts exported to GitHub for domain %s", domain_id)
        return True
    finally:
        cleanup()


async def _push_via_api(content: str, repo: str, branch: str,
                        token: str, domain_id: str) -> bool:
    """Push via GitHub Contents API (create/update file)."""
    import base64
    import httpx

    path = "facts/domain_facts.yaml"
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # Check if file exists (need SHA for update)
        sha = ""
        try:
            resp = await client.get(url, headers=headers, params={"ref": branch})
            if resp.status_code == 200:
                sha = resp.json().get("sha", "")
        except Exception:
            pass

        # Create or update
        body = {
            "message": f"bot: export domain facts ({domain_id})",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        try:
            resp = await client.put(url, headers=headers, json=body)
            if resp.status_code in (200, 201):
                log.info("Facts pushed to GitHub API for %s", domain_id)
                return True
            log.warning("GitHub API push failed for %s: %s %s",
                        domain_id, resp.status_code, resp.text[:200])
        except Exception as e:
            log.error("GitHub API push error for %s: %s", domain_id, e)

    return False
