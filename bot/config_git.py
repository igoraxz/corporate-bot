# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Git-based versioning for config files in DATA_DIR/config/.

Provides per-write git commits for state config files, enabling full
rollback history. Local-only repo (never pushed to any remote).

Defense-in-depth:
- .gitignore blocks credential file patterns
- pre-commit hook rejects staged credential files
- Physical isolation (credentials in separate directory)

Performance: ~80-110ms per git add+commit. At 10-100 config writes/day,
this adds 1-11 seconds of total overhead per day. Negligible.

Added after the relay_sessions.json wipe incident (Apr 29 2026).
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# .gitignore content — blocks credential patterns even if accidentally placed in config/
_GITIGNORE = """\
# Defense-in-depth: block credential files from ever being committed.
# Primary isolation is physical (credentials/ dir). This is the safety net.
*.session*
*.token*
*.secret*
*credentials*
*auth_state*
*msal*
*.pem
*.key
*.p12
oauth_profiles/
google-workspace-creds/
ms365-mcp/
tg_session/
camofox-profiles/
"""

# pre-commit hook — final defense layer
_PRE_COMMIT_HOOK = """\
#!/bin/bash
# Block credential files from being committed to config git repo.
# Defense-in-depth: .gitignore should already block these, but this
# catches force-adds and gitignore misconfigurations.
# NOTE: pattern must NOT match config state files like mac_ip_cache.json.
# Use specific patterns (msal_cache, token_cache) not bare "cache".
BLOCKED="oauth_token|secret|credential|\\.session|\\.key|\\.pem|msal_cache|token_cache|auth_state"
STAGED=$(git diff --cached --name-only 2>/dev/null)
if echo "$STAGED" | grep -qiE "$BLOCKED"; then
    echo "BLOCKED: credential file detected in staged changes:"
    echo "$STAGED" | grep -iE "$BLOCKED"
    exit 1
fi
"""


def init_config_repo(config_dir: str | Path) -> bool:
    """Initialize a local git repo in the config directory.

    Idempotent — skips if .git/ already exists.
    Creates .gitignore and pre-commit hook.
    Returns True if repo is ready (new or existing).
    """
    config_dir = Path(config_dir)
    git_dir = config_dir / ".git"

    if not config_dir.exists():
        log.warning("[config-git] Config dir %s does not exist, skipping git init", config_dir)
        return False

    # Clean stale index.lock (left by crashed git process)
    lock_file = git_dir / "index.lock"
    if lock_file.exists():
        try:
            lock_file.unlink()
            log.info("[config-git] Removed stale index.lock")
        except OSError:
            pass

    if not git_dir.exists():
        # First-time init
        try:
            subprocess.run(
                ["git", "init"],
                cwd=str(config_dir), capture_output=True, text=True, timeout=10,
            )
            # Set user identity (required for commits)
            subprocess.run(
                ["git", "config", "user.name", "corporate-bot"],
                cwd=str(config_dir), capture_output=True, timeout=5,
            )
            subprocess.run(
                ["git", "config", "user.email", "bot@example.com"],
                cwd=str(config_dir), capture_output=True, timeout=5,
            )
            log.info("[config-git] Initialized git repo in %s", config_dir)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("[config-git] Failed to init git repo: %s", e)
            return False

    # Write/update .gitignore
    gitignore_path = config_dir / ".gitignore"
    try:
        gitignore_path.write_text(_GITIGNORE)
    except OSError as e:
        log.warning("[config-git] Failed to write .gitignore: %s", e)

    # Write/update pre-commit hook
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    try:
        hook_path.write_text(_PRE_COMMIT_HOOK)
        hook_path.chmod(0o755)
    except OSError as e:
        log.warning("[config-git] Failed to write pre-commit hook: %s", e)

    # Initial commit if repo is empty (no commits yet)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(config_dir), capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            # No commits yet — do initial commit.
            # Use explicit file list (not -A) to avoid staging files that
            # the pre-commit hook would reject (mac_ip_cache.json etc.)
            json_files = [f.name for f in config_dir.glob("*.json")
                          if not f.name.endswith(".prev.json")]
            if json_files:
                subprocess.run(
                    ["git", "add", "--"] + json_files,
                    cwd=str(config_dir), capture_output=True, timeout=10,
                )
            subprocess.run(
                ["git", "add", "--", ".gitignore"],
                cwd=str(config_dir), capture_output=True, timeout=10,
            )
            commit_r = subprocess.run(
                ["git", "commit", "-m", "initial: config repo", "--allow-empty"],
                cwd=str(config_dir), capture_output=True, text=True, timeout=10,
            )
            if commit_r.returncode == 0:
                log.info("[config-git] Created initial commit")
            else:
                # Pre-commit hook may have blocked — reset staging to clean state
                subprocess.run(
                    ["git", "reset"],
                    cwd=str(config_dir), capture_output=True, timeout=5,
                )
                # Retry with --allow-empty and no files staged
                subprocess.run(
                    ["git", "commit", "--allow-empty", "-m", "initial: empty config repo"],
                    cwd=str(config_dir), capture_output=True, text=True, timeout=10,
                )
                log.info("[config-git] Created initial commit (empty — pre-commit blocked files)")
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("[config-git] Initial commit failed: %s", e)

    return True


def git_commit_file(config_dir: str | Path, filename: str, store_name: str,
                    entry_count: int = -1) -> None:
    """Stage and commit a single config file change.

    Called by JsonConfigStore._save() when git_tracked=True.
    Non-blocking failure — if git fails, the config write still succeeds.
    """
    config_dir = Path(config_dir)
    if not (config_dir / ".git").exists():
        return  # No repo initialized

    count_str = f" ({entry_count} entries)" if entry_count >= 0 else ""
    message = f"auto: {store_name}{count_str}"

    try:
        # Stage the specific file
        subprocess.run(
            ["git", "add", "--", filename],
            cwd=str(config_dir), capture_output=True, timeout=10,
        )
        # Also stage .gitignore if it changed
        subprocess.run(
            ["git", "add", "--", ".gitignore"],
            cwd=str(config_dir), capture_output=True, timeout=5,
        )
        # Commit (skip if nothing staged)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(config_dir), capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            # There are staged changes — commit
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(config_dir), capture_output=True, text=True, timeout=15,
            )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("[config-git] git commit failed for %s: %s", filename, e)
