# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""One-time migration: move config files to config/ and credentials/ subdirectories.

Called from main.py lifespan on every boot. Idempotent — skips files that are
already in the target location. Uses atomic move (os.rename) within the same
volume for safety.

Added after the relay_sessions.json wipe incident (Apr 29 2026) to physically
isolate credential files from state config files.
"""

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Files to move to DATA_DIR/config/ (state configs, git-versioned)
_CONFIG_FILES = [
    "relay_sessions.json",
    "external_chats.json",
    "wa_external_chats.json",
    "harnesses.json",
    "chat_harnesses.json",
    "scheduled_tasks.json",
    "model_overrides.json",
    "oauth_overrides.json",
    "external_tool_overrides.json",
    "mac_ip_cache.json",
    "oauth_default_label.json",
]

# Directories to move to DATA_DIR/credentials/ (sensitive, never git-tracked)
_CREDENTIAL_DIRS = [
    "oauth_profiles",
    "google-workspace-creds",
    "ms365-mcp",
    "tg_session",
]

# Files to move to DATA_DIR/credentials/
_CREDENTIAL_FILES = [
    "elena_google_auth_state.json",
    "ms365_auth_state.json",
]


def migrate_config_layout(data_dir: str | Path) -> None:
    """Migrate flat DATA_DIR layout to config/ + credentials/ subdirectories.

    Idempotent: safe to call on every boot. Skips items already migrated.
    Logs all actions for audit trail.
    """
    data_dir = Path(data_dir)
    config_dir = data_dir / "config"
    creds_dir = data_dir / "credentials"

    # Create target directories
    config_dir.mkdir(parents=True, exist_ok=True)
    creds_dir.mkdir(parents=True, exist_ok=True)

    migrated = 0

    # --- Move state config files ---
    for fname in _CONFIG_FILES:
        src = data_dir / fname
        dst = config_dir / fname
        if src.exists() and not src.is_symlink() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
                log.info("[config-migration] Moved %s → config/%s", fname, fname)
                migrated += 1
            except OSError as e:
                log.error("[config-migration] Failed to move %s: %s", fname, e)
        # Also move .prev.json sidecars if they exist
        prev_src = data_dir / fname.replace(".json", ".prev.json")
        prev_dst = config_dir / fname.replace(".json", ".prev.json")
        if prev_src.exists() and not prev_dst.exists():
            try:
                shutil.move(str(prev_src), str(prev_dst))
            except OSError:
                pass

    # --- Move credential directories ---
    # Some dirs (google-workspace-creds) may be Docker bind mounts — can't move.
    # In that case, create a symlink from the new path to the old (bind-mounted) path.
    # NOTE: dst may already exist as an empty directory because config.py pre-creates
    # OAUTH_PROFILES_DIR and TG_SESSION_DIR at import time (before lifespan runs the
    # migration). Treat empty dst as "needs migration" — move src contents into it.
    for dname in _CREDENTIAL_DIRS:
        src = data_dir / dname
        dst = creds_dir / dname
        if dst.is_symlink():
            continue  # already migrated to a symlink
        # If dst exists with content, we're done — don't clobber.
        if dst.exists() and any(dst.iterdir()):
            continue
        if not (src.exists() and not src.is_symlink()):
            continue  # nothing at src to migrate
        # If dst exists empty, move src contents into it (then remove src).
        if dst.exists():
            try:
                for entry in src.iterdir():
                    shutil.move(str(entry), str(dst / entry.name))
                src.rmdir()
                log.info("[config-migration] Merged %s/* → credentials/%s/ (dst was empty)",
                         dname, dname)
                migrated += 1
                continue
            except OSError as e:
                log.error("[config-migration] Failed to merge %s/ contents: %s", dname, e)
                continue
        # dst doesn't exist — try direct move, fall back to symlink for bind mounts.
        try:
            shutil.move(str(src), str(dst))
            log.info("[config-migration] Moved %s/ → credentials/%s/", dname, dname)
            migrated += 1
        except OSError:
            try:
                os.symlink(str(src), str(dst))
                log.info("[config-migration] Symlinked credentials/%s → %s (bind mount)",
                         dname, src)
                migrated += 1
            except OSError as e2:
                log.error("[config-migration] Failed to move or symlink %s/: %s", dname, e2)

    # --- Move credential files ---
    for fname in _CREDENTIAL_FILES:
        src = data_dir / fname
        dst = creds_dir / fname
        if src.exists() and not src.is_symlink() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
                log.info("[config-migration] Moved %s → credentials/%s", fname, fname)
                migrated += 1
            except OSError as e:
                log.error("[config-migration] Failed to move %s: %s", fname, e)

    # --- Set permissions on credentials dir ---
    try:
        os.chmod(str(creds_dir), 0o700)
        for root, dirs, files in os.walk(str(creds_dir)):
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o600)
                except OSError:
                    pass
    except OSError as e:
        log.warning("[config-migration] Failed to set credentials permissions: %s", e)

    if migrated:
        log.info("[config-migration] Migration complete: %d items moved", migrated)
    else:
        log.debug("[config-migration] No migration needed — layout already current")
