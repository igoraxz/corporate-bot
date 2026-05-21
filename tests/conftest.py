# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Pytest conftest — test isolation for the corporate-bot project.

CRITICAL: This file sets DATA_DIR to a temporary directory BEFORE any bot
modules are imported. This prevents tests from touching real production data
(relay_sessions.json, harnesses.json, conversations.db, etc.).

DB tables are created via raw sqlite3 (no bot module imports) to avoid
import side effects while ensuring aiosqlite threads never crash on
missing tables.
"""

import os
import signal
import shutil
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Per-test timeout — prevents any single test from hanging forever
# ---------------------------------------------------------------------------

def _alarm_handler(signum, frame):
    raise TimeoutError("Test exceeded 30-second time limit")


@pytest.fixture(autouse=True)
def _test_timeout():
    """Kill any test that takes more than 30 seconds."""
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(30)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old_handler)


@pytest.fixture(autouse=True)
def _reset_db_singleton():
    """Reset the DB singleton after each test.

    bot.storage.db uses a singleton aiosqlite connection bound to one event loop.
    pytest-asyncio creates new event loops between tests. A stale singleton bound
    to a dead loop blocks forever on any DB operation — this is the root cause of
    the full-suite hang at ~78%. Resetting _db forces a fresh connection per loop.
    """
    yield
    try:
        import bot.storage.db as db_mod
        if db_mod._db is not None:
            # Can't await close() here (sync fixture), just null the reference.
            # The old connection's thread will be cleaned up by os._exit at session end.
            db_mod._db = None
            db_mod._db_init_lock = None
    except (ImportError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# DATA_DIR isolation — MUST run before ANY bot module import
# ---------------------------------------------------------------------------

_test_data_dir = tempfile.mkdtemp(prefix="pytest_botdata_")
os.environ["DATA_DIR"] = _test_data_dir

for subdir in [
    "config", "credentials", "credentials/oauth_profiles",
    "credentials/tg_session", "credentials/google-workspace-creds",
    "credentials/ms365-mcp", "tmp", "harness-projects", "media_cache", "logs",
]:
    os.makedirs(os.path.join(_test_data_dir, subdir), exist_ok=True)

_empty_json = "{}"
for config_file in [
    "config/harnesses.json", "config/chat_harnesses.json",
    "config/external_chats.json", "config/wa_external_chats.json",
    "config/relay_sessions.json", "config/scheduled_tasks.json",
    "config/model_overrides.json", "config/oauth_overrides.json",
    "config/external_tool_overrides.json", "config/mac_ip_cache.json",
    "config/oauth_default_label.json",
]:
    filepath = os.path.join(_test_data_dir, config_file)
    if not os.path.exists(filepath):
        with open(filepath, "w") as f:
            f.write(_empty_json)


# ---------------------------------------------------------------------------
# DB initialization — raw SQL only, NO bot module imports
# ---------------------------------------------------------------------------

def _init_test_db():
    """Create ALL bot tables via raw sqlite3 so aiosqlite threads never crash.

    DDL extracted directly from bot schema source files. No bot imports.
    """
    db_path = os.path.join(_test_data_dir, "conversations.db")
    conn = sqlite3.connect(db_path)

    conn.executescript("""
        -- bot/storage/memory.py
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, user_name TEXT NOT NULL, user_id TEXT,
            text TEXT NOT NULL, role TEXT NOT NULL, timestamp TEXT NOT NULL,
            message_id TEXT, session_id TEXT,
            msg_type TEXT DEFAULT 'user_message', media_cache_id INTEGER);
        CREATE TABLE IF NOT EXISTS media_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL, original_filename TEXT,
            media_type TEXT NOT NULL, source TEXT NOT NULL,
            chat_id TEXT, sender_name TEXT, description TEXT DEFAULT '',
            file_size INTEGER DEFAULT 0, mime_type TEXT DEFAULT '',
            cached_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            file_path TEXT NOT NULL, tg_file_id TEXT);
        CREATE TABLE IF NOT EXISTS message_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_key TEXT NOT NULL, fact_value TEXT NOT NULL,
            category TEXT DEFAULT 'general', source_msg_id INTEGER,
            source_chunk_id INTEGER, doc_pointer TEXT,
            superseded_by INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, expired INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_message_facts_key ON message_facts(fact_key);
        CREATE INDEX IF NOT EXISTS idx_message_facts_category ON message_facts(category);
        CREATE VIRTUAL TABLE IF NOT EXISTS message_facts_fts USING fts5(
            fact_key, fact_value, category,
            content=message_facts, content_rowid=id);
        CREATE TRIGGER IF NOT EXISTS message_facts_ai AFTER INSERT ON message_facts BEGIN
            INSERT INTO message_facts_fts(rowid, fact_key, fact_value, category)
            VALUES (new.id, new.fact_key, new.fact_value, new.category);
        END;
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL, timestamp TEXT NOT NULL,
            model TEXT, cost_usd REAL, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
            cache_create_tokens INTEGER DEFAULT 0, num_turns INTEGER DEFAULT 0,
            duration_s REAL DEFAULT 0, query_preview TEXT);

        -- bot/storage/rag.py
        CREATE TABLE IF NOT EXISTS rag_chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, chat_id TEXT NOT NULL DEFAULT '',
            start_msg_id INTEGER NOT NULL, end_msg_id INTEGER NOT NULL,
            senders TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT NOT NULL,
            chunk_text TEXT NOT NULL, embedding BLOB NOT NULL,
            model TEXT NOT NULL DEFAULT 'gemini-embedding-001',
            created_at TEXT NOT NULL);

        -- bot/storage/domains.py
        CREATE TABLE IF NOT EXISTS domain_registry (
            domain_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
            config_yaml TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            last_sync_at TEXT, chunk_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS domain_documents (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain_id TEXT NOT NULL, file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
            last_indexed_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(domain_id, file_path));

        -- bot/storage/domain_optimize.py
        CREATE TABLE IF NOT EXISTS domain_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain_id TEXT NOT NULL, target_layer TEXT NOT NULL,
            rec_type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
            evidence TEXT DEFAULT '', status TEXT DEFAULT 'pending',
            reviewed_by TEXT DEFAULT '', review_notes TEXT DEFAULT '',
            created_at TEXT NOT NULL, reviewed_at TEXT DEFAULT '',
            applied_at TEXT DEFAULT '');

        -- bot/storage/artifacts.py
        CREATE TABLE IF NOT EXISTS coding_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL, description TEXT DEFAULT '',
            language TEXT DEFAULT '', domain_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '', commit_hash TEXT DEFAULT '',
            ticket_ref TEXT DEFAULT '', file_size INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
            file_path, description, language, domain_id,
            content=coding_artifacts, content_rowid=id);
        CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON coding_artifacts BEGIN
            INSERT INTO artifacts_fts(rowid, file_path, description, language, domain_id)
            VALUES (new.id, new.file_path, new.description, new.language, new.domain_id);
        END;

        -- bot/rbac/schema.py
        CREATE TABLE IF NOT EXISTS rbac_roles (
            role_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            description TEXT DEFAULT '', is_builtin BOOLEAN DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rbac_policies (
            policy_id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_id TEXT NOT NULL, privilege TEXT NOT NULL,
            object_type TEXT NOT NULL, object_id TEXT DEFAULT '*',
            effect TEXT DEFAULT 'allow', conditions TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS rbac_assignments (
            assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
            role_id TEXT NOT NULL, granted_by TEXT DEFAULT '',
            granted_at TEXT NOT NULL, expires_at TEXT,
            UNIQUE(subject_type, subject_id, role_id));
        CREATE TABLE IF NOT EXISTS rbac_access_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, subject_id TEXT NOT NULL,
            privilege TEXT NOT NULL, object_type TEXT NOT NULL,
            object_id TEXT NOT NULL, effect TEXT NOT NULL,
            policy_id INTEGER, role_id TEXT DEFAULT '',
            session_key TEXT DEFAULT '', tool_name TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_rbac_assignments_subject
            ON rbac_assignments(subject_type, subject_id);

        -- bot/pm/schema.py
        CREATE TABLE IF NOT EXISTS pm_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS pm_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key TEXT NOT NULL, name TEXT NOT NULL,
            key TEXT NOT NULL DEFAULT '', brief TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active', budget_usd REAL,
            spent_usd REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS pm_sprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES pm_projects(id),
            name TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft', budget_usd REAL,
            spent_usd REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS pm_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sprint_id INTEGER NOT NULL REFERENCES pm_sprints(id),
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'backlog',
            tier TEXT NOT NULL DEFAULT 'significant',
            priority INTEGER NOT NULL DEFAULT 100,
            estimated_cost_usd REAL, assignee TEXT,
            budget_usd REAL, spent_usd REAL NOT NULL DEFAULT 0.0, tcd TEXT,
            ticket_number INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS pm_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES pm_tickets(id),
            from_status TEXT NOT NULL, to_status TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'pm', reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        CREATE TABLE IF NOT EXISTS pm_agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES pm_tickets(id),
            agent_role TEXT NOT NULL, model TEXT,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            duration_s REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'running', error TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            completed_at TEXT);
        CREATE TABLE IF NOT EXISTS pm_deploys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES pm_projects(id),
            commit_from TEXT, commit_to TEXT,
            action TEXT NOT NULL DEFAULT 'rebuild',
            status TEXT NOT NULL DEFAULT 'triggered', reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            completed_at TEXT);
        CREATE TABLE IF NOT EXISTS pm_deploy_tickets (
            deploy_id INTEGER NOT NULL REFERENCES pm_deploys(id),
            ticket_id INTEGER NOT NULL REFERENCES pm_tickets(id),
            linked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (deploy_id, ticket_id));

        -- bot/onboarding/schema.py
        CREATE TABLE IF NOT EXISTS user_onboarding (
            user_id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'telegram',
            display_name TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            current_step TEXT DEFAULT '', completed_steps TEXT DEFAULT '[]',
            skipped_steps TEXT DEFAULT '[]', step_data TEXT DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS domain_access_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, domain_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL, reviewed_by TEXT DEFAULT '',
            reviewed_at TEXT, note TEXT DEFAULT '');

        -- bot/auth/
        CREATE TABLE IF NOT EXISTS user_tokens (
            user_id TEXT PRIMARY KEY, token_bundle TEXT NOT NULL,
            scopes TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL DEFAULT 0,
            refresh_expires_at REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active', acquired_at TEXT NOT NULL,
            refreshed_at TEXT, user_email TEXT NOT NULL DEFAULT '',
            user_name TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS user_oauth_tokens (
            user_id TEXT NOT NULL, provider TEXT NOT NULL,
            token TEXT NOT NULL, metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, provider));
        CREATE TABLE IF NOT EXISTS chat_oauth_tokens (
            chat_id TEXT NOT NULL, provider TEXT NOT NULL,
            token TEXT NOT NULL, set_by TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (chat_id, provider));
        CREATE TABLE IF NOT EXISTS credential_store (
            id TEXT PRIMARY KEY, type TEXT NOT NULL,
            account TEXT NOT NULL, scope TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '', encrypted_data TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

        -- bot/audit.py
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, user_id TEXT NOT NULL,
            user_email TEXT NOT NULL DEFAULT '', user_name TEXT NOT NULL DEFAULT '',
            conversation_id TEXT NOT NULL DEFAULT '',
            conversation_type TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
            domains_accessed TEXT NOT NULL DEFAULT '',
            tools_used TEXT NOT NULL DEFAULT '',
            tokens_used INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok', error TEXT NOT NULL DEFAULT '');

        -- bot/pipeline/task_queue.py
        CREATE TABLE IF NOT EXISTS task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL, chat_id TEXT NOT NULL,
            user_name TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
            parsed_json TEXT NOT NULL DEFAULT '{}',
            is_catchup INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT);
    """)

    conn.close()


_init_test_db()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def pytest_sessionfinish(session, exitstatus):
    """Clean up temp dir + force-exit on leaked threads."""
    try:
        shutil.rmtree(_test_data_dir, ignore_errors=True)
    except Exception:
        pass

    import threading
    leaked = [t for t in threading.enumerate()
              if t.is_alive() and t.name != "MainThread"]
    if leaked:
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exitstatus)
