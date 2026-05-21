#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Snapshot production SQLite DB for staging use.

Creates a WAL-safe backup of the prod database into the staging data volume.
Runs from the HOST or inside the prod container (reads /app/data/conversations.db).

Usage:
    python3 scripts/staging_snapshot_db.py [--src /path/to/conversations.db] [--dst /path/to/staging.db]

Defaults:
    --src  /app/data/conversations.db          (prod DB inside container)
    --dst  staging-snapshot.db                  (written next to src by default)

The backup uses sqlite3.Connection.backup() which is WAL-safe (consistent
point-in-time snapshot even while the bot is running).
"""

import argparse
import os
import re
import sqlite3
import sys
import time

# Default categories to scrub (all facts in these categories are deleted)
_DEFAULT_SCRUB_CATEGORIES = frozenset({
    "credentials", "passwords", "auth", "tokens",
})

# Regex patterns for sensitive values (matched against fact key + value columns)
_SCRUB_VALUE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),          # OpenAI/Anthropic API keys
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"),      # Anthropic API keys (ant- prefix)
    re.compile(r"eyJ[a-zA-Z0-9_-]{20,}"),          # JWT / base64-encoded tokens
    re.compile(r"AKIA[A-Z0-9]{16}"),               # AWS access key IDs
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),            # GitHub PATs
    re.compile(r"xox[bpras]-[a-zA-Z0-9-]+"),       # Slack tokens
    re.compile(r"1//[a-zA-Z0-9_-]{20,}"),          # Google OAuth refresh tokens
    re.compile(r"ya29\.[a-zA-Z0-9_-]{20,}"),       # Google OAuth access tokens
    re.compile(r"AIzaSy[a-zA-Z0-9_-]{30,}"),       # Google API keys
    re.compile(r"tskey-[a-zA-Z0-9_-]{20,}"),       # Tailscale auth keys
    re.compile(r"SG\.[a-zA-Z0-9_-]{20,}"),         # SendGrid API keys
    re.compile(r"-----BEGIN.*PRIVATE KEY-----"),     # PEM private keys
    re.compile(r"(api[_-]?key|secret|password|token|credential)", re.I),  # Generic keywords
]


def snapshot(src_path: str, dst_path: str, *, scrub: bool = True,
             extra_categories: set[str] | None = None) -> str:
    """Create a WAL-safe backup of src_path to dst_path.

    Args:
        src_path: Path to production SQLite database.
        dst_path: Path for the backup copy.
        scrub: If True, remove potentially sensitive data (API keys in facts).

    Returns:
        Absolute path to the created backup.
    """
    if not os.path.exists(src_path):
        print(f"ERROR: Source DB not found: {src_path}", file=sys.stderr)
        sys.exit(1)

    # Remove existing backup if present
    if os.path.exists(dst_path):
        os.remove(dst_path)

    t0 = time.time()
    src_conn = sqlite3.connect(src_path, timeout=30)
    dst_conn = sqlite3.connect(dst_path)

    try:
        # WAL-safe backup (consistent snapshot)
        src_conn.backup(dst_conn)
        elapsed = time.time() - t0
        print(f"Backup complete: {src_path} -> {dst_path} ({elapsed:.1f}s)")
    finally:
        src_conn.close()
        dst_conn.close()

    if scrub:
        _scrub_sensitive_data(dst_path, extra_categories=extra_categories)

    # Report size
    size_mb = os.path.getsize(dst_path) / (1024 * 1024)
    print(f"Snapshot size: {size_mb:.1f} MB")

    return os.path.abspath(dst_path)


def _scrub_sensitive_data(db_path: str, *,
                         extra_categories: set[str] | None = None) -> None:
    """Remove potentially sensitive data from the backup.

    Scrubs:
    - Facts in scrub categories (credentials, passwords, auth, tokens, + extras)
    - Facts whose key or value matches sensitive regex patterns
    - Message content is preserved (needed for testing context flows)
    """
    categories = _DEFAULT_SCRUB_CATEGORIES | (extra_categories or set())

    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit for VACUUM
    try:
        conn.execute("BEGIN")
        total_scrubbed = 0

        # 1. Delete facts by category (parameterized SQL)
        placeholders = ",".join("?" for _ in categories)
        cursor = conn.execute(
            f"DELETE FROM message_facts WHERE category IN ({placeholders})",
            list(categories),
        )
        total_scrubbed += cursor.rowcount

        # 2. Delete facts matching sensitive patterns in key or value
        rows = conn.execute(
            "SELECT id, fact_key, fact_value FROM message_facts"
        ).fetchall()
        pattern_ids = []
        for row_id, key, value in rows:
            for pat in _SCRUB_VALUE_PATTERNS:
                if pat.search(key or "") or pat.search(value or ""):
                    pattern_ids.append(row_id)
                    break
        if pattern_ids:
            id_placeholders = ",".join("?" for _ in pattern_ids)
            conn.execute(
                f"DELETE FROM message_facts WHERE id IN ({id_placeholders})",
                pattern_ids,
            )
            total_scrubbed += len(pattern_ids)

        conn.execute("COMMIT")
        if total_scrubbed:
            print(f"Scrubbed {total_scrubbed} sensitive fact(s) "
                  f"({len(categories)} categories + {len(pattern_ids)} pattern matches)")

        # VACUUM must run outside a transaction
        conn.execute("VACUUM")
    except sqlite3.OperationalError as e:
        # Table might not exist in a fresh DB
        print(f"Note: scrub skipped ({e})")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Snapshot prod DB for staging")
    parser.add_argument("--src", default="/app/data/conversations.db", help="Source DB path")
    parser.add_argument("--dst", default=None, help="Destination path (default: staging-snapshot.db next to src)")
    parser.add_argument("--no-scrub", action="store_true", help="Skip sensitive data removal")
    parser.add_argument("--scrub-categories", default="",
                        help="Additional categories to scrub (comma-separated)")
    args = parser.parse_args()

    dst = args.dst
    if dst is None:
        dst = os.path.join(os.path.dirname(args.src), "staging-snapshot.db")

    extra = set(c.strip() for c in args.scrub_categories.split(",") if c.strip()) if args.scrub_categories else None
    result = snapshot(args.src, dst, scrub=not args.no_scrub, extra_categories=extra)
    print(f"Ready: {result}")


if __name__ == "__main__":
    main()
