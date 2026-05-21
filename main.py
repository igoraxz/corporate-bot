# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Corporate Bot — FastAPI application with Claude Agent SDK.

Supports MS Teams + Telegram Bot API, parallel task processing
(MAX_PARALLEL_TASKS per chat, MAX_TOTAL_TASKS global),
real-time TG streaming (thinking + tools + text in one placeholder),
smart cancel, and self-diagnostics.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env before importing config
load_dotenv()

from fastapi import FastAPI, Request, Response
import uvicorn

from config import (
    HOST, PORT, TG_CHAT_ID,
    DATA_DIR, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CREDENTIALS_PATH,
    MAX_PARALLEL_TASKS, MAX_TOTAL_TASKS, get_model_primary, ORG_TIMEZONE,
    MEDIA_DESCRIPTION_ENABLED, MEDIA_DESCRIPTION_BACKFILL_LIMIT,
    ADMIN_USERS,
    TG_API_ID, TG_API_HASH, TG_SESSION_DIR, TG_SESSION_NAME,
    IS_STAGING,
)

TZ = ZoneInfo(ORG_TIMEZONE)

# Git commit hash — defined in config.py, imported here for health endpoint
from config import GIT_COMMIT

# === LOGGING (with rotation: 10MB max, 5 backups) ===
LOG_DIR = Path(os.environ.get("LOG_DIR", DATA_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    str(LOG_DIR / "bot.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=50,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)
_file_handler.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)
_console_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_file_handler, _console_handler],
)

# Silence noisy third-party loggers (reduces log volume ~80%, preserving 7+ days of history)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.WARNING)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

# === STATE FILES ===
STARTUP_FILE = DATA_DIR / "last_startup.txt"

# === EXTRACTED MODULES (re-exports for backwards compatibility) ===
from bot.pipeline.task_manager import ActiveTask, TaskManager, task_manager  # noqa: F401
from bot.pipeline.streaming import (  # noqa: F401
    _kill_placeholder, _cancel_if_pending, _tg_safe_edit, _animated_dots,
    _build_tg_status, _check_sdk_liveness, _tg_ticker,
    _get_task_timeout,
)
from bot.pipeline.staging import (  # noqa: F401
    _staging_responses, staging_buffer_response, staging_signal_completion,
    _staging_completion_events, _check_staging_auth,
    _STAGING_MAX_RESPONSES_PER_CHAT, _STAGING_MAX_CHATS,
)
from bot.pipeline.staging import test_inject as _staging_test_inject  # noqa: F401
from bot.pipeline.staging import test_get_responses as _staging_test_get_responses  # noqa: F401
from bot.pipeline.staging import test_clear_responses as _staging_test_clear_responses  # noqa: F401
from bot.pipeline.staging import test_reset_clients as _staging_test_reset_clients  # noqa: F401
from bot.pipeline.staging import test_logs as _staging_test_logs  # noqa: F401
from bot.relay.ux import (  # noqa: F401
    _TURN_SEPARATOR, _KNOWN_CLAUDE_SLASHES,
    _relay_placeholders, _pending_relay_queue,
    _TG_SEND_RETRY_BACKOFFS_S, _DESKTOP_USER_REPLY_TTL_S, _CATCHUP_REFINEMENT_TTL_S,
    _send_with_retry, mark_relay_repinned, mark_relay_recovered,
    _forward_realtime_desktop_turn, _send_desktop_catchup,
    _relay_send_file, _fmt_relay_elapsed, _handle_relay_message,
    _desktop_last_user_msg_id, _recently_repinned_chats,
    _recently_recovered_chats, _catchup_last_delivered_ts,
)
from bot.pipeline.handlers import (  # noqa: F401
    handle_telegram_message,
)
from bot.pipeline.dispatch import (  # noqa: F401
    _EC_HOT_SECS, _EC_COLD_SECS,
    _ec_bot_send_ts, _ec_throttle_handles, _ec_throttle_pending,
    update_ec_bot_send_time, _is_ec_hot, _is_active_ec_group,
    _schedule_ec_throttle, _flush_ec_throttle,
    _dispatch_task, triage_and_enqueue,
)
from bot.commands import (  # noqa: F401, E501
    _DIAGNOSE_RE, _STATUS_RE, _RESET_RE, _AUTH_RE,
    _MAC_STATUS_RE, _MAC_REAUTH_RE, _MAC_ACCOUNTS_RE, _MAC_SET_ACCOUNT_RE,
    _MAC_OAUTH_CODE_RE, _pending_mac_reauth, _PENDING_MAC_REAUTH_TTL_S,
    _MAC_LOCK_RE, _CHATS_RE, _USAGE_RE, _CLAUDE_ACCOUNTS_RE,
    _DAEMONS_RE, _DAEMON_REAP_RE, _DAEMON_KILL_RE,
    _CREATE_RELAY_RE, _DELETE_RELAY_RE, _DELETE_CHAT_RE, _CLAUDE_AUTH_RE,
    _ROLLBACK_RE, _HARNESSES_RE, _SET_HARNESS_RE,
    _HELP_RE, _SESSION_RE, _MAC_SESSIONS_RE,
    _CANCEL_RE, _CANCEL_STRICT_RE, _DROP_CALL_RE, _CONTINUE_RE,
    _cancel_prefixes_tuple, _is_cancel_message,
    _pty_drain_read, _pty_drain_loop,
    _friendly_daemon_error,
    _send_to_platform_simple, _send_to_platform_from_task,
    _task_checkpoint_loop, _suggest_diagnose_on_failure,
    _lightweight_auth, _lightweight_mac_status, _lightweight_mac_reauth,
    _handle_mac_reauth_code, _lightweight_mac_accounts, _lightweight_mac_set_account,
    _lightweight_mac_lock, _lightweight_chats, _lightweight_usage,
    _lightweight_claude_accounts, _lightweight_daemons,
    _lightweight_daemon_reap, _lightweight_daemon_kill,
    _lightweight_create_relay, _auto_cleanup_stale_relay_chats,
    _lightweight_delete_relay, _lightweight_delete_chat,
    _lightweight_claude_auth, _lightweight_rollback,
    _lightweight_harnesses, _lightweight_set_harness,
    _lightweight_help, _lightweight_mac_sessions, _lightweight_session,
    _lightweight_status, _handle_reset_session, run_self_diagnose,
)

# === PLAYWRIGHT MCP SSE SERVER ===
# Shared browser instance — all SDK clients connect via SSE, each gets a tab.
_playwright_process: asyncio.subprocess.Process | None = None
PLAYWRIGHT_MCP_PORT = os.environ.get("PLAYWRIGHT_MCP_PORT", "3001")
PLAYWRIGHT_BROWSER_PROFILE = str(DATA_DIR / "browser-profile")

# Shared across integrations/telegram.py and the enrichment call sites below.
# Defined in config.py.


def clean_browser_locks(*, kill_processes: bool = True):
    """Remove stale Chromium Singleton lock files that prevent browser startup.

    Args:
        kill_processes: If True, also kill stale chromium processes (only at startup/shutdown).
                       False when called from resource watchdog (SSE server manages processes).
    """
    profile = Path(PLAYWRIGHT_BROWSER_PROFILE)
    cleaned = 0
    for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        p = profile / lock_file
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
                cleaned += 1
            except Exception as _exc:
                log.debug("Suppressed: %s", _exc)
    if kill_processes:
        # Kill stale chromium processes from a previous crashed run.
        # Only safe at startup (before SSE server starts) or shutdown (after it stops).
        import subprocess
        my_pid = str(os.getpid())
        try:
            result = subprocess.run(
                ["pgrep", "-f", "chromium"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                pids = [p for p in result.stdout.strip().split("\n") if p != my_pid]
                for pid in pids:
                    try:
                        subprocess.run(["kill", "-9", pid], timeout=5)
                    except Exception as _exc:
                        log.debug("Suppressed: %s", _exc)
                if pids:
                    log.info(f"[Playwright] Killed {len(pids)} stale chromium process(es)")
        except Exception as _exc:
            log.debug("Suppressed: %s", _exc)
    if cleaned:
        log.info(f"[Playwright] Cleaned {cleaned} stale lock file(s)")


async def _start_playwright_server():
    """Start Playwright MCP as a shared SSE server."""
    global _playwright_process
    clean_browser_locks(kill_processes=True)

    cmd = [
        "npx", "-y", "@playwright/mcp@0.0.68",
        "--headless",
        "--browser", "chromium",
        "--user-data-dir", PLAYWRIGHT_BROWSER_PROFILE,
        "--shared-browser-context",
        "--viewport-size", "1920x1080",
        "--image-responses", "omit",
        "--output-dir", str(DATA_DIR / "tmp"),
        "--host", "0.0.0.0",
        "--allowed-hosts", "*",
        "--port", PLAYWRIGHT_MCP_PORT,
    ]
    log.info(f"[Playwright] Starting SSE server on port {PLAYWRIGHT_MCP_PORT}...")
    _playwright_process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Wait for server to be ready (HTTP health check with retries)
    import httpx
    ready = False
    for attempt in range(15):  # up to ~15 seconds
        await asyncio.sleep(1)
        if _playwright_process.returncode is not None:
            stderr = ""
            if _playwright_process.stderr:
                stderr = (await _playwright_process.stderr.read()).decode(errors="replace")
            log.error(f"[Playwright] SSE server crashed on start:\n{stderr}")
            _playwright_process = None
            return
        try:
            async with httpx.AsyncClient() as http:
                async with http.stream("GET", f"http://127.0.0.1:{PLAYWRIGHT_MCP_PORT}/sse", timeout=3.0) as resp:
                    if resp.status_code in (200, 403, 405):
                        ready = True
                        break
        except Exception:
            if attempt < 14:
                continue
    if ready:
        log.info(f"[Playwright] SSE server ready (PID {_playwright_process.pid})")
    else:
        log.warning(f"[Playwright] SSE server started (PID {_playwright_process.pid}) but health check inconclusive — browser tools may fail initially")


async def _stop_playwright_server():
    """Stop the Playwright MCP SSE server."""
    global _playwright_process
    if _playwright_process and _playwright_process.returncode is None:
        log.info("[Playwright] Stopping SSE server...")
        _playwright_process.terminate()
        try:
            await asyncio.wait_for(_playwright_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            _playwright_process.kill()
            await _playwright_process.wait()
        log.info("[Playwright] SSE server stopped")
    _playwright_process = None
    clean_browser_locks(kill_processes=True)

# Track bot startup time — messages before this are stale (context-only, not actionable)
_BOT_STARTUP_TIME: float = time.time()

# Track last stale-relay cleanup time (0 = never run, triggers on first scheduler tick)
_relay_cleanup_last: float = 0.0
_last_token_prerefresh: float = 0.0

# Persistent task queue — initialized in lifespan, used by triage_and_enqueue + handlers
persistent_queue = None  # PersistentQueue, initialized in lifespan


# === STARTUP VALIDATION ===

# _promote_admin_external_chats — REMOVED (CB-28).
# Chat access is now fully managed via unified chat_registry + RBAC.
# No "promotion" concept — admin chats get admin harness directly.


def validate_startup():
    """Check critical config before starting."""
    creds_file = CLAUDE_CREDENTIALS_PATH
    if creds_file.exists():
        log.info("  Auth: file-based credentials (~/.claude/.credentials.json) — auto-refresh enabled")
    elif CLAUDE_CODE_OAUTH_TOKEN:
        log.info("  Auth: CLAUDE_CODE_OAUTH_TOKEN env var (no auto-refresh)")
    else:
        log.info("  Auth: No credentials found!")
        log.info("  Run scripts/refresh_server_token.sh, or: docker exec -it <container> su -c 'claude auth login' botuser")

    # Check userbot session (degraded mode if missing — no TG, but health endpoint works)
    session_file = Path(TG_SESSION_DIR) / f"{TG_SESSION_NAME}.session"
    if session_file.exists():
        log.info(f"  [TG] Userbot session: {session_file}")
    else:
        log.warning(f"  [TG] No session file at {session_file} — TG will be disabled")
        log.warning("  [TG] Run: docker exec -it <container> python /host-repo/scripts/setup_tg_userbot.py")
    if TG_API_ID and TG_API_HASH:
        log.info("  [TG] API credentials: set")
    else:
        log.warning("  [TG] TG_API_ID and/or TG_API_HASH not set!")
    log.info(f"  [TG] Chat ID: {TG_CHAT_ID}")

    from config import GOOGLE_WORKSPACE_CREDS_DIR
    cred_files = list(GOOGLE_WORKSPACE_CREDS_DIR.glob("*.json"))
    if not cred_files:
        log.warning("  [Google] No workspace-mcp credentials found")
    else:
        log.info(f"  [Google] Workspace MCP credentials: {[f.name for f in cred_files]}")

    from config import VAPI_API_KEY, VAPI_PHONE_NUMBER_ID
    if VAPI_API_KEY and VAPI_PHONE_NUMBER_ID:
        log.info("  [Phone] Vapi configured")
    else:
        log.warning("  [Phone] Vapi not configured")

    try:
        import claude_agent_sdk  # noqa: F401
        log.info("  [SDK] Claude Agent SDK available")
    except ImportError:
        log.error("  [SDK] Claude Agent SDK NOT installed!")
        sys.exit(1)


# === SCHEDULED TASKS ===



async def cleanup_tmp_files():
    """Remove temp files older than 1 hour."""
    try:
        now_ts = time.time()
        tmp_dir = DATA_DIR / "tmp"
        if not tmp_dir.exists():
            return
        for fpath in tmp_dir.iterdir():
            if fpath.is_file() and (now_ts - fpath.stat().st_mtime) > 3600:
                fpath.unlink(missing_ok=True)
                log.info(f"Cleaned up temp file: {fpath.name}")
    except Exception as e:
        log.warning(f"Temp cleanup error: {e}")


async def cleanup_expired_media():
    """Remove cached media files that have exceeded the retention period."""
    try:
        from bot.storage.memory import cleanup_expired_media as do_cleanup
        await do_cleanup()
    except Exception as e:
        log.warning(f"Media cache cleanup error: {e}")


# Re-export for backward compatibility (callers in this file use _generate_media_description)
from bot.storage.media import generate_media_description as _generate_media_description


async def _backfill_media_descriptions():
    """Startup task: describe any cached media that lacks a description.

    Handles crash recovery — if the bot restarted before descriptions were generated,
    or if description generation was added after media was already cached.
    """
    if not MEDIA_DESCRIPTION_ENABLED:
        log.info("[Media] Auto-descriptions disabled")
        return
    try:
        from bot.storage.memory import get_undescribed_media
        # Fetch one more than limit to detect if there's a backlog
        undescribed = await get_undescribed_media(limit=MEDIA_DESCRIPTION_BACKFILL_LIMIT + 1)
        if not undescribed:
            log.info("[Media] All cached media has descriptions — nothing to backfill")
            return
        has_more = len(undescribed) > MEDIA_DESCRIPTION_BACKFILL_LIMIT
        batch = undescribed[:MEDIA_DESCRIPTION_BACKFILL_LIMIT]
        if has_more:
            log.info(f"[Media] Backfilling {len(batch)} media files (more remain — will continue on next restart)")
        else:
            log.info(f"[Media] Backfilling descriptions for {len(batch)} media files...")
        success = 0
        for item in batch:
            fpath = item.get("file_path", "")
            if fpath and Path(fpath).exists():
                await _generate_media_description(item["id"], fpath, item.get("filename", ""))
                success += 1
                await asyncio.sleep(0.5)  # rate-limit Gemini calls
            else:
                log.debug(f"Skipping backfill for id={item['id']}: file not found at {fpath}")
        log.info(f"[Media] Backfill complete: {success}/{len(batch)} described" + (" (more items pending)" if has_more else ""))
    except Exception as e:
        log.error(f"Media description backfill error: {e}")


async def prerefresh_google_tokens():
    """Pre-refresh Google OAuth tokens nearing expiry.

    workspace-mcp's refresh-on-demand sometimes fails transiently at the expiry
    boundary — the refresh succeeds (file updated) but the enclosing tool call
    still returns AUTH_ERROR. By proactively refreshing tokens 10 minutes before
    expiry, the MCP subprocess always finds a valid token on disk.
    """
    import json as _json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds_dir = os.environ.get(
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        str(DATA_DIR / "google-workspace-creds"),
    )
    if not os.path.isdir(creds_dir):
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    refresh_threshold = 600  # 10 minutes in seconds

    for fname in os.listdir(creds_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(creds_dir, fname)
        try:
            with open(fpath) as f:
                data = _json.load(f)

            expiry_str = data.get("expiry")
            refresh_token = data.get("refresh_token")
            if not refresh_token or not expiry_str:
                continue

            # Parse expiry — skip non-ISO values (e.g. bare integers)
            try:
                expiry = datetime.fromisoformat(str(expiry_str))
                if expiry.tzinfo is not None:
                    expiry = expiry.replace(tzinfo=None)
            except (ValueError, TypeError):
                continue

            remaining = (expiry - now).total_seconds()
            if remaining > refresh_threshold:
                continue

            # Build Credentials object and refresh
            creds = Credentials(
                token=data.get("token"),
                refresh_token=refresh_token,
                token_uri=data.get("token_uri"),
                client_id=data.get("client_id"),
                client_secret=data.get("client_secret"),
                scopes=data.get("scopes"),
                expiry=expiry,
            )

            await asyncio.to_thread(creds.refresh, Request())

            # Save refreshed token back — atomic write
            data["token"] = creds.token
            data["expiry"] = creds.expiry.isoformat() if creds.expiry else None
            if creds.refresh_token:
                data["refresh_token"] = creds.refresh_token

            tmp_path = fpath + ".tmp"
            with open(tmp_path, "w") as f:
                _json.dump(data, f, indent=2)
            os.replace(tmp_path, fpath)

            log.info(f"[Token prerefresh] Refreshed {fname} (was {remaining:.0f}s from expiry)")
        except Exception as e:
            log.warning(f"[Token prerefresh] Failed for {fname}: {e}")


async def prerefresh_m365_token():
    """Pre-refresh M365 MSAL token nearing expiry.

    Reads the Softeria envelope cache, checks access token expiry, uses the
    refresh token to get a new access token from Azure AD, and writes back.
    This keeps the refresh token alive (90-day sliding window) and ensures
    the MCP server always finds a valid token on disk.
    """
    import httpx

    from config import MS365_MCP_CLIENT_ID, MS365_MCP_CLIENT_SECRET, MS365_MCP_TENANT_ID
    ms365_client_id = MS365_MCP_CLIENT_ID
    ms365_tenant_id = MS365_MCP_TENANT_ID
    ms365_client_secret = MS365_MCP_CLIENT_SECRET
    if not ms365_client_id or not ms365_tenant_id:
        return

    # Read from the primary cache path (Softeria reads from credentials/)
    cache_dir = "/app/data/credentials/ms365-mcp"
    cache_file = os.path.join(cache_dir, "msal_cache.json")
    if not os.path.exists(cache_file):
        # Try alternate path
        cache_file = "/app/data/ms365-mcp/msal_cache.json"
        if not os.path.exists(cache_file):
            return

    try:
        with open(cache_file) as f:
            envelope = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[M365 prerefresh] Cannot read cache: {e}")
        return

    if not envelope.get("_cacheEnvelope"):
        return
    try:
        cache_data = json.loads(envelope["data"])
    except (json.JSONDecodeError, KeyError):
        return

    # Find access token and check expiry
    at_entries = cache_data.get("AccessToken", {})
    if not at_entries:
        return
    at_key = next(iter(at_entries))
    at_info = at_entries[at_key]
    try:
        expires_on = int(at_info.get("expires_on", "0"))
    except (ValueError, TypeError):
        return
    now = int(time.time())
    remaining = expires_on - now

    # Only refresh if within 10 minutes of expiry (same as Google)
    if remaining > 600:
        return

    # Find refresh token
    rt_entries = cache_data.get("RefreshToken", {})
    if not rt_entries:
        log.warning("[M365 prerefresh] No refresh token in cache — cannot refresh")
        return
    rt_key = next(iter(rt_entries))
    refresh_token = rt_entries[rt_key].get("secret", "")
    if not refresh_token:
        return

    scopes_no_offline = at_info.get("target", "")
    authority = f"https://login.microsoftonline.com/{ms365_tenant_id}"

    log.info(f"[M365 prerefresh] Token expires in {remaining}s — refreshing...")

    try:
        async with httpx.AsyncClient(timeout=30) as sess:
            resp = await sess.post(
                f"{authority}/oauth2/v2.0/token",
                data={
                    "client_id": ms365_client_id,
                    "client_secret": ms365_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": f"{scopes_no_offline} offline_access".strip(),
                },
            )
            if resp.status_code != 200:
                log.warning(f"[M365 prerefresh] Refresh failed (HTTP {resp.status_code})")
                return
            result = resp.json()
    except Exception as e:
        log.warning(f"[M365 prerefresh] HTTP error: {e}")
        return

    # Update cache with new tokens
    new_at = result.get("access_token", "")
    new_rt = result.get("refresh_token", "")
    if not new_at:
        log.warning("[M365 prerefresh] No access_token in refresh response")
        return

    new_now = int(time.time())
    new_expires_on = new_now + result.get("expires_in", 3600)
    new_ext_expires = new_now + result.get("ext_expires_in", result.get("expires_in", 3600))

    # Update access token in cache
    at_info["secret"] = new_at
    at_info["cached_at"] = str(new_now)
    at_info["expires_on"] = str(new_expires_on)
    at_info["extended_expires_on"] = str(new_ext_expires)

    # Update refresh token if rotated
    if new_rt:
        rt_entries[rt_key]["secret"] = new_rt

    # Update ID token if present
    new_id = result.get("id_token", "")
    if new_id:
        id_entries = cache_data.get("IdToken", {})
        if id_entries:
            id_key = next(iter(id_entries))
            id_entries[id_key]["secret"] = new_id

    # Rewrite envelope
    now_ms = int(time.time() * 1000)
    new_envelope = json.dumps({
        "_cacheEnvelope": True,
        "data": json.dumps(cache_data),
        "savedAt": now_ms,
    })

    # Write to BOTH paths atomically
    for dir_path in ["/app/data/ms365-mcp", "/app/data/credentials/ms365-mcp"]:
        out_file = os.path.join(dir_path, "msal_cache.json")
        if os.path.isdir(dir_path):
            tmp = out_file + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(new_envelope)
                os.replace(tmp, out_file)
            except OSError as e:
                log.warning(f"[M365 prerefresh] Write failed for {dir_path}: {e}")

    log.info(f"[M365 prerefresh] Token refreshed (was {remaining}s from expiry, new expires in {result.get('expires_in', '?')}s)")


async def check_custom_tasks():
    """Check and execute any due custom scheduled tasks."""
    from bot.scheduler import get_due_tasks, mark_task_run
    from bot.agent import process_system_task
    from bot.prompts import build_scheduled_task_prompt

    due_tasks = get_due_tasks()
    for task in due_tasks:
        task_id = task["id"]
        task_name = task["name"]
        platform = task.get("platform") or ""
        if not platform:
            from config import ROOT_ADMIN
            platform = ROOT_ADMIN[0] or "telegram"  # CB-132: inherit from ROOT_ADMIN
        task_chat_id = task.get("chat_id", "")
        log.info(f">>> Firing scheduled task: {task_name} ({task_id}), chat_id={task_chat_id or 'admin'}")
        try:
            # For Teams tasks: restore service_url so TeamsClient can send proactively
            if platform == "teams" and task_chat_id:
                try:
                    from integrations.teams import get_client as _get_teams_client
                    from config import TEAMS_EXTERNAL_CHATS
                    _tc = _get_teams_client()
                    _teams_entry = TEAMS_EXTERNAL_CHATS.get(task_chat_id)
                    if _tc and _teams_entry and _teams_entry.get("service_url"):
                        _tc.store_service_url(task_chat_id, _teams_entry["service_url"])
                        log.debug("Restored Teams service_url for scheduled task: %s", task_chat_id[:20])
                except Exception as _tse:
                    log.debug("Teams service_url restore failed (non-blocking): %s", _tse)
            prompt = build_scheduled_task_prompt(task_name, task["prompt"], platform)
            await process_system_task(prompt, chat_id=task_chat_id)
            mark_task_run(task_id)
            log.info(f"Scheduled task completed: {task_name}")
        except Exception as e:
            log.error(f"Scheduled task failed ({task_name}): {e}")
            mark_task_run(task_id)  # Mark as run to avoid retry loop


async def scheduler_loop():
    """Background loop for periodic tasks."""
    log.info("Scheduler started")
    cleanup_counter = 0
    while True:
        try:
            await check_custom_tasks()
            cleanup_counter += 1
            # Cleanup expired sessions + stale JSONL files every ~10 min (20 x 30s ticks)
            if cleanup_counter % 20 == 0:
                await cleanup_tmp_files()
                from bot.agent import client_pool
                await client_pool.cleanup_expired()
                client_pool.cleanup_stale_session_files()
                await client_pool.cleanup_stale_session_map()
            # Resource watchdog every ~5 min (10 x 30s ticks)
            if cleanup_counter % 10 == 0:
                from bot.agent import client_pool
                await client_pool.resource_watchdog()
            # TG connection watchdog every ~2.5 min (5 x 30s ticks)
            # Skip in staging -- TG is not started.
            # In Bot API mode, just verify the client is alive (stateless HTTP).
            # In userbot mode, reconnect if Pyrogram MTProto dropped.
            if cleanup_counter % 5 == 3 and not IS_STAGING:
                try:
                    from integrations.telegram import is_connected, reconnect
                    if not is_connected():
                        _tg_watchdog_mode = os.environ.get("TG_MODE", "bot")
                        log.warning("[TG watchdog] %s disconnected -- attempting reconnect",
                                    "Bot API" if _tg_watchdog_mode == "bot" else "Pyrogram")
                        ok = await reconnect()
                        if not ok:
                            log.error("[TG watchdog] Reconnect failed -- will retry in ~2.5 min")
                except Exception as e:
                    log.error(f"[TG watchdog] Check failed: {e}")
            # Pre-refresh Google + M365 OAuth tokens — every ~50 min
            global _last_token_prerefresh
            if time.time() - _last_token_prerefresh > 3000:
                try:
                    await prerefresh_google_tokens()
                except Exception as e:
                    log.warning(f"[Token prerefresh] Google scheduler error: {e}")
                try:
                    await prerefresh_m365_token()
                except Exception as e:
                    log.warning(f"[Token prerefresh] M365 scheduler error: {e}")
                # Per-user token vault pre-refresh (H5 fix)
                try:
                    from bot.auth.token_vault import prerefresh_expiring_tokens
                    await prerefresh_expiring_tokens(buffer_minutes=10, batch_size=50)
                except Exception as e:
                    log.warning(f"[Token prerefresh] User vault error: {e}")
                _last_token_prerefresh = time.time()
            # Mac desktop health watchdog — every tick (~30s).
            # Detects: offline→online, auth transitions, unhealthy daemon sweep.
            try:
                from bot.desktop_relay import mac_health_watchdog, is_enabled as _mac_enabled
                if _mac_enabled():
                    await mac_health_watchdog()
            except Exception as e:
                log.debug(f"[Mac watchdog] Check failed: {e}")
            # Stale relay chat cleanup — once per day
            if time.time() - _relay_cleanup_last > 86400:
                try:
                    from bot.desktop_relay import is_enabled as _relay_enabled
                    if _relay_enabled():
                        asyncio.create_task(_auto_cleanup_stale_relay_chats())
                except Exception as e:
                    log.debug("[Relay cleanup] Scheduler check failed: %s", e)
            # RAG incremental chunk update every ~5 min
            if cleanup_counter % 10 == 8:
                try:
                    from bot.storage.rag import update_chunks_incremental
                    await update_chunks_incremental()
                except Exception as e:
                    log.warning(f"RAG incremental update failed: {e}")
            if cleanup_counter >= 60:
                cleanup_counter = 0
                await cleanup_expired_media()
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        await asyncio.sleep(30)


# Tool labels and send-tool set — single source of truth in hooks.py


# === TASK MANAGER — extracted to bot/pipeline/task_manager.py ===
# ActiveTask, TaskManager, task_manager imported at top of file

# Fork context injection buffer removed — sequential per-chat model (no forks)


# === TG STREAMING + WA STATUS — extracted to bot/pipeline/streaming.py ===
# _kill_placeholder, _cancel_if_pending, _tg_safe_edit, _animated_dots,
# _build_tg_status, _check_sdk_liveness, _tg_ticker,
#
# _get_task_timeout imported at top of file (from bot.pipeline.streaming)



# === MESSAGE PROCESSING — extracted to bot/pipeline/handlers.py, bot/relay/ux.py ===
# handle_telegram_message -> bot/pipeline/handlers.py
# _handle_relay_message, _send_desktop_catchup, etc. -> bot/relay_ux.py

# === LONG-RUNNING TASK CHECKPOINTS — extracted to bot/commands.py ===
# _task_checkpoint_loop, _send_to_platform_from_task -> bot/commands.py

# === SLASH COMMAND HANDLERS — extracted to bot/commands.py ===
# All _lightweight_* functions, regex patterns, run_self_diagnose,
# _handle_reset_session, _suggest_diagnose_on_failure -> bot/commands.py

# === MESSAGE TRIAGE & DISPATCH — extracted to bot/pipeline/dispatch.py ===
# _dispatch_task, triage_and_enqueue, DC throttle -> bot/pipeline/dispatch.py


# === PERSISTENT QUEUE INIT ===

async def _init_persistent_queue():
    """Initialize the PersistentQueue: schema, recovery, GC, worker."""
    global persistent_queue
    from bot.pipeline.task_queue import PersistentQueue
    persistent_queue = PersistentQueue(
        dispatch_fn=_dispatch_task,
        can_accept_fn=task_manager.can_accept_task,
        excluded_chats_fn=task_manager.get_over_limit_chats,
    )
    await persistent_queue.init_schema()
    await persistent_queue.startup_recovery()
    await persistent_queue.gc_old_rows(hours=24)
    log.info("[PQ] Persistent task queue initialized")


# === FASTAPI APP ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup and shutdown."""
    log.info("=" * 60)
    log.info("Corporate Bot (Agent SDK) starting...")
    log.info(f"  Max parallel tasks: {MAX_PARALLEL_TASKS}/chat, {MAX_TOTAL_TASKS} total")

    validate_startup()

    # --- Init: Config/credentials directory migration (one-time, idempotent) ---
    from bot.config_migration import migrate_config_layout
    migrate_config_layout(DATA_DIR)

    # --- Init: Config git repo (local-only versioning for rollback) ---
    from bot.config_git import init_config_repo
    init_config_repo(DATA_DIR / "config")

    # --- Init: Staging OAuth credentials (copy from prod volume) ---
    if IS_STAGING:
        _prod_creds = Path("/prod-claude/.credentials.json")
        _staging_creds = Path.home() / ".claude" / ".credentials.json"
        if _prod_creds.exists():
            _staging_creds.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            try:
                # Remove existing file first — may be owned by root from volume init
                if _staging_creds.exists():
                    _staging_creds.unlink()
                shutil.copy2(str(_prod_creds), str(_staging_creds))
                log.info(f"[STAGING] Copied OAuth credentials from {_prod_creds}")
            except PermissionError:
                log.warning(f"[STAGING] Permission denied copying OAuth credentials to {_staging_creds} — SDK may lack OAuth")
        else:
            log.warning("[STAGING] No prod credentials at /prod-claude/.credentials.json — SDK may lack OAuth")
        # Also copy OAuth profiles from prod bot-data volume (mounted at /prod-data)
        _prod_profiles = Path("/prod-data/credentials/oauth_profiles")
        if not _prod_profiles.is_dir():
            _prod_profiles = Path("/prod-data/oauth_profiles")  # pre-migration fallback
        _staging_profiles = Path(DATA_DIR) / "credentials" / "oauth_profiles"
        if _prod_profiles.is_dir():
            _staging_profiles.mkdir(parents=True, exist_ok=True)
            import shutil
            _copied = 0
            for pf in _prod_profiles.glob("*.json"):
                try:
                    _dst = _staging_profiles / pf.name
                    if _dst.exists():
                        _dst.unlink()
                    shutil.copy2(str(pf), str(_dst))
                    _copied += 1
                except Exception as _e:
                    log.warning(f"[STAGING] Failed to copy OAuth profile {pf.name}: {_e}")
            if _copied:
                log.info(f"[STAGING] Copied {_copied} OAuth profile(s) from prod")
        else:
            log.info("[STAGING] No prod OAuth profiles at /prod-data/oauth_profiles — using credentials.json only")

    log.info("=" * 60)

    # --- Init: Database ---
    from bot.storage.memory import init_db
    await init_db()

    # --- Init: Persistent task queue ---
    await _init_persistent_queue()

    # --- Init: PM deploy lifecycle — resolve deploys left in 'triggered' status ---
    try:
        from bot.pm.store import resolve_pending_deploys
        pm_resolve = await resolve_pending_deploys()
        if pm_resolve.get("resolved"):
            log.info(f"PM deploy resolved on startup: {pm_resolve}")
    except Exception as e:
        log.warning(f"PM deploy resolution failed (non-blocking): {e}")

    # --- Init: Chat registry migration (populate from legacy registries) ---
    try:
        from bot.chat_registry import migrate_from_legacy
        _migrated = migrate_from_legacy()
        if _migrated:
            log.info(f"Chat registry: {_migrated} entries migrated from legacy")
    except Exception as e:
        log.warning(f"Chat registry migration failed (non-blocking): {e}")

    # --- Init: Scheduled tasks (seed defaults if needed) ---
    from bot.scheduler import init_default_tasks
    init_default_tasks()

    # --- Init: Restore session map ---
    from bot.agent import client_pool
    await client_pool.restore_clients()

    # --- Init: Relay realtime Desktop→TG forwarder (item 2) ---
    # Pool dispatches desktop_turn events from each daemon's tailer to this
    # callback. Safe to set regardless of whether DESKTOP_ENABLED — pool only
    # calls it if a daemon is actually spawned.
    from bot.relay.pool import set_desktop_turn_forwarder, set_catchup_forwarder
    set_desktop_turn_forwarder(_forward_realtime_desktop_turn)

    async def _forward_catchup(chat_id: str, msg: dict) -> None:
        mt = msg.get("type")
        if mt == "catchup":
            await _send_desktop_catchup(
                chat_id, msg.get("entries") or [],
                msg.get("earlier_count") or 0,
                msg.get("earlier_summary") or "",
                is_initial=bool(msg.get("is_initial")),
                cause=msg.get("cause") or "",
                tail_truncated=bool(msg.get("tail_truncated")),
            )
        elif mt == "catchup_warning":
            from integrations.telegram import send_message as _tg_send
            _u = msg.get("uuid") or "?"
            _s = msg.get("subtype") or "unknown"
            await _send_with_retry(
                lambda: _tg_send(
                    f"\u26a0\ufe0f <b>Desktop catchup skipped</b>\n"
                    f"No JSONL on Mac for pinned session <code>{_u}</code> "
                    f"(<i>{_s}</i>).", chat_id=int(chat_id), parse_mode="HTML"),
                label="catchup_warning", chat_id=chat_id)
    set_catchup_forwarder(_forward_catchup)

    # --- Init: Eager relay daemon spawn ---
    # Reconnect daemons for all relay_mode=true chats on startup.
    # This ensures catchup fires immediately (no wait for first user msg)
    # and the realtime tail loop starts forwarding Desktop turns right away.
    async def _eager_spawn_relay_daemons():
        from bot.desktop_relay import is_enabled as _relay_enabled, get_all_links
        if not _relay_enabled():
            return
        from bot.relay.pool import get_pool
        links = await get_all_links()
        spawned = 0
        for cid, entry in links.items():
            if not entry.get("relay_mode"):
                continue
            jsonl_path = entry.get("pinned_jsonl_path") or ""
            if not jsonl_path:
                continue
            # Extract uuid from path
            import os.path as _osp
            uuid = _osp.basename(jsonl_path).replace(".jsonl", "")
            cwd = entry.get("project_path") or ""
            try:
                pool = get_pool()
                client = await pool.acquire(cid, uuid, cwd)
                await asyncio.wait_for(client.ensure_ready(), timeout=30)
                spawned += 1
                log.info("Eager relay spawn: chat=%s uuid=%s", cid, uuid[:8])
            except Exception as e:
                log.warning("Eager relay spawn failed: chat=%s err=%s", cid, e)
        if spawned:
            log.info("Eager relay spawn: %d daemons reconnected", spawned)
    asyncio.create_task(_eager_spawn_relay_daemons())

    # --- Init: RAG v4 chunk tables ---
    from bot.storage.rag import init_rag_tables
    await init_rag_tables()

    # --- Init: Corporate knowledge domains (C1, C4) ---
    from bot.storage.domains import init_domain_tables, load_domains_from_dir, seed_canonical_domains
    await init_domain_tables()
    from config import DATA_DIR as _dd
    _domains_loaded = await load_domains_from_dir(_dd / "domains")
    _canonical_seeded = await seed_canonical_domains()
    log.info(f"  [Domains] {_domains_loaded} config + {_canonical_seeded} canonical domain(s)")
    # Init domain optimization tables (CB-102) + artifact registry (CB-104)
    try:
        from bot.storage.domain_optimize import init_optimization_tables
        await init_optimization_tables()
        from bot.storage.artifacts import init_artifact_tables
        await init_artifact_tables()
    except Exception as e:
        log.warning(f"Domain optimization/artifact tables init failed: {e}")

    # Create scheduled tasks for domain maintenance (CB-113)
    try:
        from bot.storage.domains import ensure_domain_scheduled_tasks
        _tasks_created = await ensure_domain_scheduled_tasks()
        if _tasks_created:
            log.info(f"  [Domains] {_tasks_created} scheduled task(s) created")
    except Exception as e:
        log.debug(f"Domain scheduled task creation failed: {e}")

    # --- Init: RBAC tables and seed builtin roles (Sprint R3) ---
    try:
        from bot.rbac.schema import init_rbac_db
        from bot.rbac.seed import seed_builtin_roles
        await init_rbac_db()
        await seed_builtin_roles()
        # Warm up RBAC cache so slash commands work on first boot
        from bot.rbac import get_engine as _get_rbac_engine
        _eng = _get_rbac_engine()
        await _eng._refresh_cache()
        log.info("  [RBAC] Initialized (RBAC-only enforcement, cache warmed)")
    except Exception as e:
        log.warning("  [RBAC] Init failed (non-blocking): %s", e)

    # --- Init: Onboarding tables ---
    try:
        from bot.onboarding.schema import init_onboarding_db
        await init_onboarding_db()
        log.info("  [Onboarding] Tables initialized")
    except Exception as e:
        log.warning("  [Onboarding] Init failed (non-blocking): %s", e)

    # Init GitHub sync client
    from config import GITHUB_TOKEN
    if GITHUB_TOKEN:
        from bot.storage.github_sync import init_sync_client
        init_sync_client(token=GITHUB_TOKEN)
        log.info("  [Domains] GitHub sync client initialized")
    else:
        log.info("  [Domains] GITHUB_TOKEN not set — domain sync disabled (manual indexing only)")

    # --- Init: Corporate per-user token vault (C5) ---
    from bot.auth.token_vault import init_token_tables
    await init_token_tables()
    log.info("  [Auth] Per-user token vault initialized")

    # --- Init: OAuth token tables (B5a — user + chat OAuth flows) ---
    from bot.auth.schema import init_auth_tables
    await init_auth_tables()
    log.info("  [Auth] OAuth token tables initialized (user + chat)")

    # --- Init: Universal credential store ---
    from bot.auth.credentials import init_credential_tables
    await init_credential_tables()
    log.info("  [Auth] Universal credential store initialized")

    # --- Init: Audit logging ---
    from bot.audit import init_audit_tables
    await init_audit_tables()
    log.info("  [Audit] Audit logging initialized")

    # --- Init: Harness auto-assignment rules ---
    from bot.harness_rules import seed_default_rules
    seed_default_rules()
    log.info("  [Rules] Harness assignment rules seeded")

    # --- Init: Media cache cleanup ---
    from config import MEDIA_RETENTION_DAYS
    log.info(f"  [Media] Cache retention: {MEDIA_RETENTION_DAYS} days")
    await cleanup_expired_media()

    # --- Init: Media description backfill (non-blocking) ---
    log.info(f"  [Media] Auto-descriptions: {'enabled' if MEDIA_DESCRIPTION_ENABLED else 'disabled'}")
    asyncio.create_task(_backfill_media_descriptions())

    # --- Init: Playwright MCP SSE server (shared browser) ---
    if IS_STAGING:
        log.info("[STAGING] Skipping Playwright server (browser disabled in staging)")
    else:
        await _start_playwright_server()

    # Legacy promotion + cleanup removed (CB-28). Chat access via chat_registry + RBAC.

    # --- Init: Telegram (mode-dependent: Bot API or Pyrogram userbot) ---
    _tg_started = False
    _tg_mode = os.environ.get("TG_MODE", "bot")
    if IS_STAGING:
        log.info("[STAGING] Skipping Telegram (TG disabled in staging)")
    elif _tg_mode == "bot":
        # Bot API mode: initialize client, set webhook for incoming messages
        from integrations.telegram import start as tg_start
        _tg_started = await tg_start()
        if _tg_started:
            log.info("[TG] Bot API client initialized")
            # Set webhook for incoming updates
            from integrations.telegram_botapi import _bot as _tg_bot_instance
            if _tg_bot_instance:
                _tg_webhook_url = os.environ.get("TG_WEBHOOK_URL", "")
                if _tg_webhook_url:
                    from config import TG_WEBHOOK_SECRET as _tg_webhook_secret
                    try:
                        _wh_kwargs: dict = {
                            "url": f"{_tg_webhook_url}/webhook/telegram",
                            "allowed_updates": ["message", "callback_query", "my_chat_member"],
                        }
                        if _tg_webhook_secret:
                            _wh_kwargs["secret_token"] = _tg_webhook_secret
                        await _tg_bot_instance.set_webhook(**_wh_kwargs)
                        log.info("[TG] Webhook set: %s/webhook/telegram", _tg_webhook_url)
                    except Exception as e:
                        log.error("[TG] Failed to set webhook: %s", e)
                else:
                    log.warning("[TG] TG_WEBHOOK_URL not set -- webhook not configured. "
                                "Bot will not receive messages until webhook is set.")
                # Register bot menu commands (visible in TG UI)
                try:
                    from integrations.telegram_botapi import set_bot_commands
                    # Default commands (visible in all chats)
                    _common_cmds = [
                        {"command": "help", "description": "Show available commands"},
                        {"command": "status", "description": "Show bot status and active tasks"},
                        {"command": "cancel", "description": "Stop the active task"},
                    ]
                    await set_bot_commands(_common_cmds, scope="default")
                    # DM-specific commands (available to ALL registered users in their DM)
                    await set_bot_commands([
                        *_common_cmds,
                        {"command": "continue", "description": "Resume a paused task"},
                        {"command": "reset", "description": "Reset your session"},
                        {"command": "knowledge", "description": "Knowledge domains"},
                        {"command": "claude_auth", "description": "Set up Claude access"},
                        {"command": "google_auth", "description": "Connect Google account"},
                        {"command": "m365_auth", "description": "Connect Microsoft 365"},
                        {"command": "github_auth", "description": "Connect GitHub"},
                        {"command": "accounts", "description": "Your connected accounts"},
                        {"command": "claude_set_oauth", "description": "Switch Claude profile"},
                        {"command": "onboarding", "description": "Setup progress"},
                    ], scope="all_private_chats")
                    # Group-specific commands
                    await set_bot_commands([
                        *_common_cmds,
                        {"command": "knowledge", "description": "Knowledge domains"},
                        {"command": "topics", "description": "List forum topics"},
                    ], scope="all_group_chats")
                    log.info("[TG] Bot menu commands registered (global scopes)")
                    # Set per-chat menus for all registered chats (RBAC-derived)
                    try:
                        from bot.command_menus import refresh_all_chat_commands
                        _menu_count = await refresh_all_chat_commands()
                        if _menu_count:
                            log.info(f"[TG] Per-chat menu commands set for {_menu_count} chats")
                    except Exception as me:
                        log.warning(f"[TG] Per-chat menu refresh failed: {me}")
                except Exception as e:
                    log.warning("[TG] Failed to register menu commands: %s", e)
        else:
            log.warning("[TG] Bot API not started -- TG messaging disabled")
    else:
        # Userbot mode: Pyrogram handles messages via MTProto handler callback
        from integrations.telegram import start as tg_start, get_users_info as tg_get_users_info
        _tg_started = await tg_start(message_callback=triage_and_enqueue)
        if _tg_started:
            log.info("[TG] Pyrogram userbot connected")
            # Fetch live usernames for team members and refresh prompt tags
            from config import PARENT_TG_IDS, update_user_tags_from_tg
            _tg_user_info = await tg_get_users_info(PARENT_TG_IDS)
            if _tg_user_info:
                update_user_tags_from_tg(_tg_user_info)
        else:
            log.warning("[TG] Pyrogram not started -- TG messaging disabled")

    # --- Init: Phone (Vapi) ---
    if not IS_STAGING:
        from config import VAPI_API_KEY
        if VAPI_API_KEY:
            log.info("[Phone] Vapi configured — call events webhook on /call/events")
        else:
            log.info("[Phone] Vapi not configured")

    # --- Init: MS Teams Bot ---
    from config import TEAMS_ENABLED, TEAMS_APP_ID, TEAMS_APP_SECRET, TEAMS_TENANT_ID
    if TEAMS_ENABLED and TEAMS_APP_ID and TEAMS_APP_SECRET and TEAMS_TENANT_ID:
        from integrations.teams import init_client as init_teams_client
        from config import TEAMS_ALLOWED_TENANTS
        init_teams_client(TEAMS_APP_ID, TEAMS_APP_SECRET, TEAMS_TENANT_ID, TEAMS_ALLOWED_TENANTS)
        log.info("[Teams] Bot initialized — webhook on /api/teams-messages")
    elif TEAMS_ENABLED:
        log.warning("[Teams] TEAMS_ENABLED=true but missing APP_ID/APP_SECRET/TENANT_ID")
    else:
        log.info("[Teams] Not configured (TEAMS_ENABLED=false)")

    # Start background tasks
    scheduler_task = asyncio.create_task(scheduler_loop())
    pq_worker_task = asyncio.create_task(persistent_queue.worker_loop())

    checkpoint_task = asyncio.create_task(_task_checkpoint_loop())

    if IS_STAGING:
        log.info("[STAGING] ======================================")
        log.info("[STAGING] STAGING MODE — TG/WA/relay disabled")
        log.info("[STAGING] Test endpoint: POST /test/inject")
        log.info("[STAGING] ======================================")
    else:
        # Send restart notification to TG group, TG admin direct, and WA group
        from config import has_undeployed_changes
        _pending_tag = " \u26a0\ufe0f UNDEPLOYED CHANGES" if has_undeployed_changes() else ""
        restart_msg = f"Bot restarted ({GIT_COMMIT}{_pending_tag})"
        # Only notify primary group + original admin DMs (from ADMIN_USERS env).
        # Promoted external chats (coding/relay groups) are in TG_ADMIN_CHAT_IDS
        # but should NOT get restart noise.
        _original_admin_dm_ids = set()
        for _src, _uid in ADMIN_USERS:
            if _src == "telegram":
                try:
                    _original_admin_dm_ids.add(int(_uid))
                except (ValueError, TypeError):
                    pass
        try:
            from integrations.telegram import send_message as tg_send
            # Send restart notification to admin DMs only (no default group)
            for admin_id in _original_admin_dm_ids:
                try:
                    await tg_send(restart_msg, chat_id=admin_id, parse_mode=None)
                except Exception as e2:
                    log.warning(f"Failed to send restart to admin {admin_id}: {e2}")
        except Exception as e:
            log.warning(f"Failed to send TG restart notification: {e}")
    # Startup smoke test: verify SDK query path works before accepting traffic.
    # Catches signature mismatches, missing imports, bad config.
    # If this fails, app crashes → health check fails → watcher auto-rolls back.
    try:
        from bot.agent import client_pool
        test_opts = client_pool._build_options(
            system_prompt="smoke test",
            model=get_model_primary(),
            session_key="smoke-test",
        )
        log.info(f"Startup smoke test passed: model={test_opts.model} budget={test_opts.max_budget_usd}")
    except Exception as e:
        log.critical(f"STARTUP SMOKE TEST FAILED: {e} — aborting startup", exc_info=True)
        raise

    # Record actual startup time (once, not per scheduler tick)
    STARTUP_FILE.write_text(datetime.now(TZ).isoformat())

    # Startup config warnings
    from config import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        log.warning("⚠️ GEMINI_API_KEY not set — RAG semantic search, media descriptions, "
                    "and document parsing are DISABLED. Get a key at https://aistudio.google.com/apikey")

    # Start report hosting server (isolated port, serves saved HTML reports)
    try:
        from bot.reports import start_report_server, stop_report_server
        await start_report_server()
    except Exception as _rpt_err:
        log.warning("Report server failed to start (non-blocking): %s", _rpt_err)

    yield

    # Shutdown
    try:
        await stop_report_server()
    except Exception as _exc:
        log.debug("Suppressed: %s", _exc)
    if persistent_queue:
        persistent_queue.stop()
    log.info("Shutting down... (pending PQ rows preserved in SQLite for restart recovery)")

    # Delete all active TG placeholders to avoid duplicates after restart
    if not IS_STAGING:
        try:
            from integrations.telegram import delete_message as tg_del
            for t in task_manager.get_active_tasks():
                if t.tg_placeholder_id and t.tg_placeholder_alive:
                    chat_id = t.chat_id or TG_CHAT_ID
                    try:
                        await asyncio.wait_for(
                            tg_del(t.tg_placeholder_id, chat_id=int(chat_id)),
                            timeout=2.0,
                        )
                        log.info(f"Shutdown: deleted placeholder {t.tg_placeholder_id} for task {t.task_id}")
                    except Exception as e:
                        log.debug(f"Shutdown: placeholder delete failed: {e}")
                    _kill_placeholder(t)
        except Exception as e:
            log.debug(f"Shutdown: placeholder cleanup failed: {e}")

    # Best-effort shutdown notification (skip in staging — no TG/WA)
    # Only notify primary group + original admin DMs (from ADMIN_USERS env).
    # Promoted external chats (coding/relay groups) are in TG_ADMIN_CHAT_IDS
    # but should NOT get shutdown noise — same pattern as restart notification.
    if not IS_STAGING:
        shutdown_msg = "Bot shutting down..."
        _shutdown_admin_dm_ids: set[int] = set()
        for _src, _uid in ADMIN_USERS:
            if _src == "telegram":
                try:
                    _shutdown_admin_dm_ids.add(int(_uid))
                except (ValueError, TypeError):
                    pass
        try:
            from integrations.telegram import send_message as tg_send
            # Send shutdown notification to admin DMs only (no default group)
            for admin_id in _shutdown_admin_dm_ids:
                try:
                    await asyncio.wait_for(
                        tg_send(shutdown_msg, chat_id=admin_id, parse_mode=None),
                        timeout=3.0,
                    )
                except Exception as e:
                    log.debug(f"Cleanup failed: {e}")
        except Exception as e:
            log.debug(f"Cleanup failed: {e}")
    for t in [scheduler_task, pq_worker_task, checkpoint_task]:
        if t is not None:
            t.cancel()

    # Drain relay pool — cancel turn listeners, delete active placeholders.
    # MUST happen BEFORE Pyrogram stop so TG API calls still work.
    try:
        from bot.relay.ux import drain_relay_placeholders
        await asyncio.wait_for(drain_relay_placeholders(), timeout=5.0)
    except Exception as e:
        log.debug("Shutdown: relay placeholder drain failed: %s", e)

    # Disconnect all SDK clients
    from bot.agent import client_pool
    await client_pool.disconnect_all()

    # Close DB singletons
    from bot.storage.db import close_db
    await close_db()
    from bot.pm.db import close_pm_db
    await close_pm_db()

    # Stop Teams client
    try:
        from integrations.teams import close_client as close_teams_client
        await close_teams_client()
    except Exception as e:
        log.debug("Shutdown: Teams client close failed: %s", e)

    # Stop Pyrogram userbot
    from integrations.telegram import stop as tg_stop
    await tg_stop()

    # Stop Playwright MCP SSE server
    await _stop_playwright_server()


app = FastAPI(title="Corporate Bot", lifespan=lifespan)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Receive Telegram Bot API updates via webhook.

    In Bot API mode (TG_MODE=bot): parses incoming Update, routes to pipeline.
    In userbot mode (TG_MODE=userbot): returns 200 (Pyrogram handles via MTProto).
    """
    tg_mode = os.environ.get("TG_MODE", "bot")
    if tg_mode != "bot":
        return Response(status_code=200)

    # Verify secret token (Bot API webhook security — REQUIRED)
    import hmac as _hmac
    from config import TG_WEBHOOK_SECRET as webhook_secret
    if not webhook_secret:
        log.error("Telegram webhook: TG_WEBHOOK_SECRET not configured — rejecting request")
        return Response(status_code=500)
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not _hmac.compare_digest(header_secret, webhook_secret):
        log.warning("Telegram webhook: invalid secret token")
        return Response(status_code=401)

    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    # Parse update using Bot API parser
    try:
        from integrations.tg_parser_botapi import parse_bot_update, parse_callback_query
        from integrations.telegram_botapi import _bot as bot_instance

        if not bot_instance:
            log.warning("Telegram webhook: Bot API client not initialized")
            return {"ok": True}

        from telegram import Update
        update = Update.de_json(data, bot_instance)

        # CB-130: Track forum topic names from service messages.
        # This runs BEFORE auth check — we track names even for unregistered topics.
        msg = update.message or update.edited_message
        if msg:
            _ftc = msg.forum_topic_created
            _fte = getattr(msg, "forum_topic_edited", None)
            if (_ftc or _fte) and msg.message_thread_id and msg.chat:
                try:
                    from bot.chat_registry import track_topic_name
                    _topic_info = _ftc or _fte
                    _name = getattr(_topic_info, "name", "") or ""
                    if _name:
                        _ic = getattr(_topic_info, "icon_color", None)
                        track_topic_name("telegram", msg.chat.id, msg.message_thread_id,
                                         _name, icon_color=_ic)
                except Exception as _e:
                    log.debug("Topic name tracking failed: %s", _e)

        parsed = parse_bot_update(update)

        # Validate chat/user is authorized before processing.
        # Bot API mode receives webhooks from ALL users who DM the bot —
        # only process messages from known chats (registry + root admin).
        from bot.chat_registry import is_authorized as _chat_is_authorized

        def _is_authorized(p: dict) -> bool:
            try:
                cid = p.get("chat_id") or 0
                uid = p.get("user_id")
                tid = p.get("message_thread_id")
                return _chat_is_authorized("telegram", cid, user_id=uid, thread_id=tid)
            except Exception:
                return False

        if parsed:
            if _is_authorized(parsed):
                await triage_and_enqueue(parsed)
            else:
                log.info("Ignoring message from unknown chat=%s user=%s",
                         parsed.get("chat_id"), parsed.get("user_id"))
        elif update.callback_query:
            # Handle inline keyboard button presses
            cq_parsed = parse_callback_query(update)
            if cq_parsed:
                if _is_authorized(cq_parsed):
                    from integrations.telegram_botapi import answer_callback_query
                    await answer_callback_query(cq_parsed["callback_query_id"])
                    await triage_and_enqueue(cq_parsed)
                else:
                    log.info("Ignoring callback_query from unknown chat=%s user=%s",
                             cq_parsed.get("chat_id"), cq_parsed.get("user_id"))
    except Exception as e:
        log.error("Telegram webhook processing error: %s", e, exc_info=True)

    return {"ok": True}


def _get_health_rate_limits() -> dict:
    """Get rate limit state for /health endpoint."""
    try:
        from bot.agent import get_rate_limit_state
        return get_rate_limit_state()
    except Exception:
        return {}


@app.get("/health")
async def health():
    """Health check endpoint."""
    from integrations.phone import get_active_calls
    from integrations.telegram import is_connected as tg_is_connected
    from bot.agent import client_pool
    active_calls = get_active_calls()
    active_tasks = task_manager.get_active_tasks()
    from config import has_undeployed_changes, _GIT_COMMIT_SOURCE
    _pending = has_undeployed_changes()
    return {
        "status": "ok",
        "version": "agent-sdk",
        "commit": GIT_COMMIT,
        "commit_source": _GIT_COMMIT_SOURCE,
        "pending_deploy": _pending,
        "timestamp": datetime.now(TZ).isoformat(),
        "pq_pending": await persistent_queue.get_pending_count() if persistent_queue else 0,
        "active_tasks": len(active_tasks),
        "max_per_chat": MAX_PARALLEL_TASKS,
        "max_total": MAX_TOTAL_TASKS,
        "tasks": [{"id": t.task_id, "text": t.text[:40], "phase": t.phase,
                    "elapsed": t.elapsed()} for t in active_tasks],
        "active_calls": len(active_calls),
        "tg_connected": tg_is_connected(),
        "clients": client_pool.get_status(),
        "rate_limits": _get_health_rate_limits(),
    }


@app.get("/health/deep")
async def health_deep():
    """Deep health check — verifies the critical query() code path.

    Unlike /health (which only checks HTTP), this endpoint exercises
    the pre-SDK portion of ClientPool.query(): harness resolution,
    prompt building, access context, and browser profile computation.
    If any of these crash (NameError, AttributeError, etc.), this
    endpoint returns 500 instead of 200 — triggering deploy rollback.

    Added after the _harness NameError incident (Apr 29 2026).
    """
    from bot.agent import ClientPool
    from bot.harness import get_harness_for_chat
    try:
        # Exercise the same code paths query() uses before reaching the SDK.
        # Use the primary chat session key as the test subject.
        _test_sk = f"telegram:{TG_CHAT_ID}"
        _h = get_harness_for_chat(_test_sk)

        # These are the exact operations that caused the _harness crash:
        # R4: project_dir removed from Harness — sandbox determined by RBAC.
        # For health check purposes, just use False (no sandbox in test).
        _has_sandbox = False
        _label = _h.label or "default"
        _pw = ClientPool._playwright_profiles_dir(_test_sk, False, _has_sandbox, _label)
        _cfx = ClientPool._camofox_profiles_dir(_test_sk, False)

        # Verify prompt building doesn't crash
        from bot.prompts import build_system_prompt
        _prompt = build_system_prompt(
            session_source="telegram",
            session_chat_id=str(TG_CHAT_ID),
            is_external_chat=False,
        )
        return {
            "status": "ok",
            "deep_check": "passed",
            "harness_label": _label,
            "prompt_length": len(_prompt),
        }
    except Exception as e:
        import traceback
        # Log full details server-side; return only error type to avoid
        # information disclosure (filesystem paths, stack traces).
        log.error("[health/deep] FAILED: %s", traceback.format_exc())
        return Response(
            content=json.dumps({
                "status": "error",
                "deep_check": "failed",
                "error": type(e).__name__,
            }),
            status_code=500,
            media_type="application/json",
        )


@app.get("/")
async def root():
    return {"name": "Corporate Bot", "status": "running", "engine": "claude-agent-sdk"}


# === PHONE CALL WEBHOOK (Vapi) ===

@app.post("/call/events")
async def call_events(request: Request):
    """Vapi server webhook: receives all call events."""
    try:
        event = await request.json()
    except Exception:
        return Response(status_code=400)

    from integrations.phone import handle_call_event
    result = handle_call_event(event)

    if result:
        duration = result.get("duration_seconds", 0)
        cost = result.get("cost_usd", 0)
        mins = duration // 60
        secs = duration % 60

        summary_parts = [
            f"📞 <b>Call completed</b> ({mins}m {secs}s, ~${cost:.2f})",
            f"<b>To:</b> {result.get('to_number')}",
            f"<b>Objective:</b> {result.get('objective')}",
            f"<b>Ended:</b> {result.get('ended_reason', 'unknown')}",
        ]

        if result.get("summary"):
            summary_parts.append(f"\n<b>Summary:</b> {result['summary']}")

        if result.get("transcript"):
            transcript = result["transcript"]
            if len(transcript) > 20000:
                transcript = transcript[:20000] + "... [truncated]"
            import html as _html
            summary_parts.append(f"\n<b>Transcript:</b>\n<pre>{_html.escape(transcript)}</pre>")

        summary = "\n".join(summary_parts)

        source = result.get("source", "telegram")
        # Corporate mode: route to the chat that initiated the call (stored in result).
        # Fallback to admin notification channels if not specified.
        chat_id = result.get("chat_jid") or result.get("chat_id") or str(TG_CHAT_ID)
        if chat_id:
            await _send_to_platform_simple(source, chat_id, summary)
        else:
            log.warning("Vapi webhook: no chat_id to send summary to")

    return Response(status_code=200)


# === MS TEAMS WEBHOOK ===

@app.post("/api/teams-messages")
async def teams_webhook(request: Request):
    """Receive incoming Activities from Azure Bot Service.

    Bot Framework protocol: Azure Bot Service sends POST with Activity JSON
    + JWT in Authorization header. We validate, parse, and route to pipeline.
    Returns 200 immediately — async processing via triage_and_enqueue.
    """
    from config import TEAMS_ENABLED
    if not TEAMS_ENABLED:
        return Response(status_code=404)

    # Rate limiting (CB-49)
    from bot.rate_limiter import webhook_limiter
    client_ip = request.client.host if request.client else "unknown"
    if not webhook_limiter.allow(client_ip):
        log.warning("Teams webhook rate limited: ip=%s", client_ip)
        return Response(status_code=429, content="Too Many Requests")

    from integrations.teams import get_client
    client = get_client()
    if not client:
        log.error("Teams webhook called but client not initialized")
        return Response(status_code=503)

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    # Validate JWT (Azure Bot Service signs all incoming webhooks)
    auth_header = request.headers.get("Authorization", "")
    try:
        await client.validate_jwt(auth_header)
    except ValueError as e:
        log.warning("Teams JWT validation failed: %s", e)
        return Response(status_code=401, content="Unauthorized")
    except Exception as e:
        log.error("Teams JWT validation error: %s", e, exc_info=True)
        return Response(status_code=401, content="Unauthorized")

    # Parse Activity to standard dict
    parsed = client.parse_activity(body)

    if parsed is None:
        # Non-message activity (conversationUpdate, typing, etc.) — handled silently
        return Response(status_code=200)

    # Handle invoke responses (SSO token exchange requires {"status": 200} body)
    if isinstance(parsed, dict) and parsed.get("_invoke_response"):
        return Response(
            status_code=200,
            content=json.dumps({"status": 200}),
            media_type="application/json",
        )

    # Skip empty messages
    if not parsed.get("text", "").strip() and not parsed.get("has_media"):
        return Response(status_code=200)

    # Register Teams external chat
    conversation_id = parsed.get("chat_id", "")
    if conversation_id:
        from config import TEAMS_EXTERNAL_CHATS, TEAMS_DEFAULT_HARNESS
        if conversation_id not in TEAMS_EXTERNAL_CHATS:
            import datetime
            TEAMS_EXTERNAL_CHATS.set(conversation_id, {
                "title": f"Teams {parsed.get('teams_conversation_type', 'chat')}",
                "mode": "active_plus",
                "added_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
                "conversation_type": parsed.get("teams_conversation_type", ""),
                "tenant_id": parsed.get("teams_tenant_id", ""),
                "service_url": parsed.get("teams_service_url", ""),  # persisted for proactive messaging
            })
            log.info(
                "Teams: registered conversation %s (type=%s)",
                conversation_id[:30],
                parsed.get("teams_conversation_type", "?"),
            )
            # Auto-assign harness via rules engine (H1 fix)
            try:
                from bot.harness import set_chat_harness
                from bot.harness_rules import resolve_harness_for_teams_chat
                teams_session_key = f"teams:{conversation_id}"
                resolved_harness = resolve_harness_for_teams_chat(parsed, TEAMS_DEFAULT_HARNESS)
                set_chat_harness(teams_session_key, resolved_harness)
                log.info(
                    "Teams: assigned harness '%s' to %s (rules engine)",
                    resolved_harness, conversation_id[:30],
                )
            except Exception as e:
                log.warning("Teams: failed to assign harness: %s", e)

    # Route to pipeline (process_incoming stores the message — no pre-store here)
    try:
        from bot.pipeline.dispatch import triage_and_enqueue
        await triage_and_enqueue(parsed)
    except Exception as e:
        log.error("Teams: triage_and_enqueue failed: %s", e, exc_info=True)

    # Return 200 immediately (5-second ACK requirement)
    return Response(status_code=200)


# === GITHUB WEBHOOK ENDPOINT (C3) ===

@app.post("/webhooks/github")
async def github_webhook(request: Request):
    """Receive GitHub push events for knowledge domain re-indexing.

    Validates HMAC-SHA256 signature, identifies affected domain,
    triggers incremental re-indexing of changed files.
    """
    # Rate limiting (CB-49)
    from bot.rate_limiter import github_webhook_limiter
    client_ip = request.client.host if request.client else "unknown"
    if not github_webhook_limiter.allow(client_ip):
        log.warning("GitHub webhook rate limited: ip=%s", client_ip)
        return Response(status_code=429, content="Too Many Requests")

    from config import GITHUB_WEBHOOK_SECRET
    body_bytes = await request.body()

    # SECURITY: Reject all webhooks if secret not configured (F2 fix)
    if not GITHUB_WEBHOOK_SECRET:
        log.error("GitHub webhook received but GITHUB_WEBHOOK_SECRET not configured — rejecting")
        return Response(status_code=500, content="Webhook secret not configured")

    # Verify HMAC-SHA256 signature (mandatory)
    from bot.storage.github_sync import verify_webhook_signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(body_bytes, signature, GITHUB_WEBHOOK_SECRET):
            log.warning("GitHub webhook: signature verification failed")
            return Response(status_code=401, content="Invalid signature")

    try:
        payload = json.loads(body_bytes)
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    # Only process push events
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type != "push":
        return Response(status_code=200, content=json.dumps({"status": "ignored", "event": event_type}))

    # Route to domain handler + trigger actual sync (H6 fix)
    from bot.storage.domains import handle_github_push
    result = await handle_github_push(payload, webhook_secret=GITHUB_WEBHOOK_SECRET)

    # If domain matched and files need indexing, trigger incremental sync
    if result.get("domain_id") and result.get("files_to_index", 0) > 0:
        domain_id = result["domain_id"]
        commits = payload.get("commits", [])
        added = set()
        modified = set()
        removed = set()
        for commit in commits:
            added.update(commit.get("added", []))
            modified.update(commit.get("modified", []))
            removed.update(commit.get("removed", []))

        from bot.storage.github_sync import sync_domain_incremental
        asyncio.create_task(sync_domain_incremental(
            domain_id, list(added | modified), list(removed),
        ))
        result["sync_triggered"] = True

    return Response(
        status_code=200,
        content=json.dumps(result),
        media_type="application/json",
    )


# === STAGING TEST ENDPOINTS ===
# Logic extracted to bot/pipeline/staging.py — thin route wrappers below delegate to it.
# _staging_responses, staging_buffer_response, staging_signal_completion,
# _staging_completion_events, _check_staging_auth imported at top of file.


@app.post("/test/inject")
async def test_inject(request: Request):
    """Inject a synthetic message into the processing pipeline (staging only)."""
    result = await _staging_test_inject(request)
    if result["body"] is None:
        return Response(status_code=result["status_code"])
    return Response(
        content=json.dumps(result["body"]),
        status_code=result["status_code"],
        media_type="application/json",
    )


@app.get("/test/responses/{chat_id}")
async def test_get_responses(chat_id: str, request: Request):
    """Get buffered staging responses for a chat (staging only)."""
    result = await _staging_test_get_responses(chat_id, request)
    if result["body"] is None:
        return Response(status_code=result["status_code"])
    return Response(
        content=json.dumps(result["body"]),
        status_code=result["status_code"],
        media_type="application/json",
    )


@app.delete("/test/responses/{chat_id}")
async def test_clear_responses(chat_id: str, request: Request):
    """Clear buffered staging responses for a chat (staging only)."""
    result = await _staging_test_clear_responses(chat_id, request)
    if result["body"] is None:
        return Response(status_code=result["status_code"])
    return Response(
        content=json.dumps(result["body"]),
        status_code=result["status_code"],
        media_type="application/json",
    )


@app.post("/test/reset-clients")
async def test_reset_clients(request: Request):
    """Reset SDK clients matching a prefix (staging only). Body: {"prefix": "qa_"}"""
    result = await _staging_test_reset_clients(request)
    if result["body"] is None:
        return Response(status_code=result["status_code"])
    return Response(
        content=json.dumps(result["body"]),
        status_code=result["status_code"],
        media_type="application/json",
    )


@app.get("/test/logs")
async def test_logs(request: Request):
    """Return raw log lines matching a grep pattern (staging only). ?pattern=X&lines=50&file=bot.log"""
    result = await _staging_test_logs(request)
    if result["body"] is None:
        return Response(status_code=result["status_code"])
    return Response(
        content=json.dumps(result["body"]),
        status_code=result["status_code"],
        media_type="application/json",
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )
