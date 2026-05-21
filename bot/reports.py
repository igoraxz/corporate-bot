"""Static HTML report hosting — isolated FastAPI server on separate port.

Serves saved HTML reports at /reports/{report_id} with UUID4-based
unguessable URLs. Runs on REPORTS_PORT (default 8210), separate from
the main bot API (port 8000) for security isolation.

Serves /reports/{id} for HTML reports and /oauth/google/callback for OAuth flows.
This port can be safely exposed to the internet.

Reports are stored per-chat: REPORTS_DIR/{chat_id}/{uuid4}.html.
Each chat's reports are isolated — sandbox chats can't see other chats' reports.
Auto-cleaned after REPORTS_MAX_AGE_DAYS (default 365) by the scheduler.
"""

import asyncio
import logging
import os
import re
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

log = logging.getLogger(__name__)

# UUID4 format validation (prevents path traversal)
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# ============================================================
# Isolated FastAPI app — ONLY report serving
# ============================================================

report_app = FastAPI(title="Report Server", docs_url=None, redoc_url=None, openapi_url=None)


@report_app.get("/reports/{report_id}")
async def serve_report(report_id: str):
    """Serve a saved HTML report by UUID4 ID.

    Security:
    - UUID4 format validated (no path traversal)
    - No directory listing
    - 122 bits of entropy (unguessable)
    """
    from config import REPORTS_DIR

    # Validate UUID4 format strictly
    if not _UUID4_RE.match(report_id):
        return Response(status_code=404)

    # Reports are stored in per-chat subdirs: REPORTS_DIR/{chat_id}/{uuid}.html
    # Scan subdirs for the UUID (UUIDs are globally unique — no collision risk)
    reports_resolved = REPORTS_DIR.resolve()
    resolved = None

    # Check flat location first (backward compat)
    flat_path = REPORTS_DIR / f"{report_id}.html"
    if flat_path.exists():
        resolved = flat_path.resolve()
    else:
        # Scan per-chat subdirectories
        try:
            for entry in os.scandir(REPORTS_DIR):
                if entry.is_dir():
                    candidate = REPORTS_DIR / entry.name / f"{report_id}.html"
                    if candidate.exists():
                        resolved = candidate.resolve()
                        break
        except Exception:
            pass

    if resolved is None:
        return Response(status_code=404)

    # Verify file is within REPORTS_DIR (defense-in-depth)
    if not str(resolved).startswith(str(reports_resolved) + os.sep):
        log.warning("Report path traversal attempt: %s", report_id)
        return Response(status_code=404)

    # Determine CSP: tg-native reports have no scripts (strict),
    # hosted reports need CDN scripts (permissive for whitelisted CDNs)
    _has_script = b"<script" in resolved.read_bytes()[:2048]
    if _has_script:
        # Hosted mode — allow specific CDNs + inline scripts
        csp = (
            "default-src 'none'; "
            "script-src 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net/npm/chart.js; "
            "style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src data: https:; "
            "font-src data: https://fonts.gstatic.com; "
            "connect-src 'none';"
        )
    else:
        # TG-native mode — no scripts at all (strictest)
        csp = (
            "default-src 'none'; "
            "style-src 'unsafe-inline'; "
            "img-src data: https:; "
            "font-src data:;"
        )

    return FileResponse(
        str(resolved),  # Use resolved path, not original (TOCTOU defense)
        media_type="text/html",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


@report_app.get("/")
async def report_root():
    """No directory listing — return 404."""
    return Response(status_code=404)


# ============================================================
# OAuth callback routes (delegated to bot/auth/oauth_callbacks.py)
# ============================================================

from bot.auth.oauth_callbacks import register_oauth_routes as _register_oauth
_register_oauth(report_app)

# Legacy re-exports for callers that import from reports.py
from bot.auth.oauth_callbacks import sign_oauth_state as _sign_oauth_state  # noqa: F401
from bot.auth.oauth_callbacks import verify_oauth_state as _verify_oauth_state  # noqa: F401


# ============================================================
# Report management functions
# ============================================================

def _sanitize_chat_id(chat_id: str) -> str:
    """Sanitize chat_id for use as directory name. Prevent traversal."""
    # Replace any path-unsafe chars with underscore
    safe = re.sub(r"[^\w\-@.]", "_", chat_id)
    # Block traversal
    safe = safe.replace("..", "_")
    return safe or "default"


def save_report(html_content: str, title: str = "", chat_id: str = "") -> dict:
    """Save an HTML report and return access info.

    Reports are stored per-chat: REPORTS_DIR/{chat_id}/{uuid}.html
    This ensures sandbox chat isolation — each chat's reports are separate.

    Args:
        html_content: Complete HTML string
        title: Optional title for logging
        chat_id: Chat ID for per-chat isolation (sanitized for filesystem)

    Returns:
        {
            "report_id": "uuid4-string",
            "file_path": "/app/data/reports/{chat_id}/uuid.html",
            "report_url": "http://host:port/reports/uuid",
        }
    """
    from config import REPORTS_DIR, REPORTS_PORT, REPORTS_BASE_URL, WEBHOOK_BASE_URL

    safe_chat = _sanitize_chat_id(chat_id)
    chat_dir = REPORTS_DIR / safe_chat
    chat_dir.mkdir(parents=True, exist_ok=True)

    # Detect file path references and resolve to actual content.
    # Models sometimes pass a file path tag instead of reading the file first.
    _resolved = html_content
    if len(_resolved) < 500 and not _resolved.strip().startswith("<"):
        # Short content that doesn't look like HTML — check if it's a file path
        import re as _re_report
        _path_match = _re_report.search(r'file_path="([^"]+)"', _resolved)
        if not _path_match:
            _path_match = _re_report.search(r'(/app/\S+\.html)', _resolved)
        if _path_match:
            from pathlib import Path as _P_report
            _ref_path = _P_report(_path_match.group(1)).resolve()
            # Security: only resolve paths under /app/data/ (sandbox + harness projects)
            if not str(_ref_path).startswith("/app/data/"):
                log.warning("save_report: rejected non-/app/data/ file reference: %s", _ref_path)
            elif _ref_path.exists() and _ref_path.stat().st_size > 100:
                log.info("save_report: resolved file reference %s (%d bytes)",
                         _ref_path, _ref_path.stat().st_size)
                _resolved = _ref_path.read_text(encoding="utf-8")

    report_id = str(uuid.uuid4())
    file_path = chat_dir / f"{report_id}.html"
    file_path.write_text(_resolved, encoding="utf-8")

    # Build URL — priority: REPORTS_BASE_URL > WEBHOOK_BASE_URL-derived > localhost
    if REPORTS_BASE_URL:
        report_url = f"{REPORTS_BASE_URL.rstrip('/')}/reports/{report_id}"
    elif WEBHOOK_BASE_URL:
        from urllib.parse import urlparse
        parsed = urlparse(WEBHOOK_BASE_URL)
        report_url = f"{parsed.scheme}://{parsed.hostname}:{REPORTS_PORT}/reports/{report_id}"
    else:
        report_url = f"http://localhost:{REPORTS_PORT}/reports/{report_id}"

    log.info(
        "Report saved: id=%s title=%s size=%dKB path=%s",
        report_id[:8], title[:30] if title else "-",
        len(html_content) // 1024, file_path,
    )

    return {
        "report_id": report_id,
        "file_path": str(file_path),
        "report_url": report_url,
    }


async def cleanup_old_reports() -> int:
    """Delete reports older than REPORTS_MAX_AGE_DAYS.

    Called by scheduler every ~10 min (alongside tmp cleanup).
    Returns count of deleted files.
    """
    from config import REPORTS_DIR, REPORTS_MAX_AGE_DAYS

    if not REPORTS_DIR.exists():
        return 0

    max_age_s = REPORTS_MAX_AGE_DAYS * 86400
    now = time.time()
    deleted = 0

    try:
        # Walk per-chat subdirs + flat legacy files
        for entry in os.scandir(REPORTS_DIR):
            if entry.is_file() and entry.name.endswith(".html"):
                # Legacy flat file
                age = now - entry.stat().st_mtime
                if age > max_age_s:
                    os.unlink(entry.path)
                    deleted += 1
            elif entry.is_dir():
                # Per-chat subdir
                try:
                    for sub_entry in os.scandir(entry.path):
                        if sub_entry.is_file() and sub_entry.name.endswith(".html"):
                            age = now - sub_entry.stat().st_mtime
                            if age > max_age_s:
                                os.unlink(sub_entry.path)
                                deleted += 1
                    # Remove empty chat dirs
                    if not any(os.scandir(entry.path)):
                        os.rmdir(entry.path)
                except Exception as e2:
                    log.debug("Report cleanup subdir error: %s", e2)
    except Exception as e:
        log.warning("Report cleanup error: %s", e)

    if deleted:
        log.info("Report cleanup: deleted %d old reports", deleted)

    return deleted


# ============================================================
# Server lifecycle
# ============================================================

_server_task: asyncio.Task | None = None
_uvicorn_server = None  # uvicorn.Server instance for graceful shutdown


async def start_report_server():
    """Start the isolated report server in a background task."""
    global _server_task, _uvicorn_server
    from config import REPORTS_PORT, REPORTS_DIR

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    import uvicorn
    config = uvicorn.Config(
        report_app,
        host="0.0.0.0",
        port=REPORTS_PORT,
        log_level="warning",
        access_log=False,
    )
    _uvicorn_server = uvicorn.Server(config)

    _server_task = asyncio.create_task(_uvicorn_server.serve())
    log.info(
        "[Reports] Server started on port %d (serving from %s)",
        REPORTS_PORT, REPORTS_DIR,
    )


async def stop_report_server():
    """Stop the report server gracefully."""
    global _server_task, _uvicorn_server
    if _uvicorn_server:
        _uvicorn_server.should_exit = True
    if _server_task and not _server_task.done():
        try:
            await asyncio.wait_for(_server_task, timeout=5.0)
        except asyncio.TimeoutError:
            _server_task.cancel()
            try:
                await _server_task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
    _server_task = None
    _uvicorn_server = None
    log.info("[Reports] Server stopped")
