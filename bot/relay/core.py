# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""High-level employee workstation control API — ping, status, authenticate, etc.

Contains config constants, connection state cache, and the high-level
functions that compose SSH, registry, and session primitives for managing
employee Mac workstation channels.
"""

import asyncio
import io
import logging
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "ping",
    "is_mac_online",
    "authenticate",
    "status",
    "lock",
    "is_enabled",
    "get_cached_status",
    "_mark_auth_expired",
    "eager_respawn_daemon",
    "install_mac_skills",
    "install_mac_daemon",
    "check_install_status",
    "update_pinned_jsonl",
    "_safe_main_attr",
    "DESKTOP_ENABLED",
    "DESKTOP_HOST",
    "DESKTOP_USER",
    "DESKTOP_SESSION_TIMEOUT",
    "_last_ping",
    "_last_ping_ok",
    "_cached_authenticated",
    "_cached_session_remaining",
    "_cached_auth_time",
    "_RELAY_QUERY_PRELUDE",
    "_SAFE_TOTP_CODE",
    "_SAFE_PATH",
]

# Strict validation for shell-interpolated parameters
_SAFE_TOTP_CODE = re.compile(r"^\d{6}$")
_SAFE_PATH = re.compile(r"^[a-zA-Z0-9_./ ~-]+$")  # no shell metacharacters

# === CONFIG (from env) ===
DESKTOP_ENABLED = os.environ.get("DESKTOP_ENABLED", "false").lower() == "true"
DESKTOP_HOST = os.environ.get("DESKTOP_HOST", "your-mac")
DESKTOP_USER = os.environ.get("DESKTOP_USER", "user")
DESKTOP_SESSION_TIMEOUT = int(os.environ.get("DESKTOP_SESSION_TIMEOUT", "7200"))

# Connection state
_last_ping: float = 0.0
_last_ping_ok: bool = False
_cached_authenticated: bool = False
_cached_session_remaining: int = 0
_cached_auth_time: float = 0.0  # when auth status was last checked

# Prelude prepended to EVERY query sent to the Mac SDK daemon.
# Apr 19 2026: Re-emptied. Prelude text appears in Desktop Claude.app's UI
# as part of user messages, making them messy. The RELAY_FILE protocol is
# taught via ~/.claude/CLAUDE.md (global, installed by install_skills).
_RELAY_QUERY_PRELUDE = ""


def _safe_main_attr(name: str, default: Any = None) -> Any:
    """Lazy-import `main` module attribute, fail-open on import error.

    Used by watchdog/kick paths in this module that need to peek at main's
    state (e.g. `_relay_tasks`, `mark_relay_recovered`) without creating
    a hard `main<->desktop_relay` import cycle. Returns `default` if main
    isn't importable yet (early startup) or if the attr is missing.
    """
    try:
        import main as _main_mod  # noqa: PLC0415 — lazy by design
        return getattr(_main_mod, name, default)
    except Exception:
        return default


def is_enabled() -> bool:
    """Check if employee workstation control feature is enabled."""
    return DESKTOP_ENABLED


def get_cached_status() -> dict:
    """Get last known status without making SSH calls.

    Auth status is cached from the last status() or authenticate() call,
    with session_remaining adjusted for elapsed time since the check.
    """
    # Estimate current session_remaining by subtracting elapsed time
    authenticated = _cached_authenticated
    session_remaining = _cached_session_remaining
    if authenticated and _cached_auth_time > 0:
        elapsed = time.monotonic() - _cached_auth_time
        session_remaining = max(0, session_remaining - int(elapsed))
        if session_remaining <= 0:
            authenticated = False  # session likely expired

    return {
        "enabled": DESKTOP_ENABLED,
        "host": DESKTOP_HOST,
        "user": DESKTOP_USER,
        "last_ping": _last_ping,
        "last_ping_ok": _last_ping_ok,
        "online": _last_ping_ok,
        "authenticated": authenticated,
        "session_remaining": session_remaining,
        "_auth_checked": _cached_auth_time > 0,  # False until first status()/authenticate()
    }


def _mark_auth_expired() -> None:
    """Mark TOTP session as expired in the auth cache.

    Called when a daemon error signals auth failure so subsequent relay
    messages can fast-fail in the preflight check without a 15-second hang.
    Sets _auth_checked=True so the preflight knows the state is known-bad,
    not merely unknown.
    """
    global _cached_authenticated, _cached_session_remaining, _cached_auth_time
    _cached_authenticated = False
    _cached_session_remaining = 0
    _cached_auth_time = time.monotonic()  # makes _auth_checked True


async def ping() -> dict:
    """Check if Mac is online via Tailscale SSH.

    relay-ping is exempt from TOTP auth (allowed by guard.sh without session).

    Returns dict with: online, latency_ms, timestamp
    """
    global _last_ping, _last_ping_ok

    from bot.relay.ssh import execute  # noqa: PLC0415

    start = time.monotonic()
    ok, output = await execute("relay-ping", timeout=10.0)
    elapsed_ms = round((time.monotonic() - start) * 1000)

    _last_ping = time.time()
    _last_ping_ok = ok and output.startswith("PONG:")

    return {
        "online": _last_ping_ok,
        "latency_ms": elapsed_ms if _last_ping_ok else None,
        "timestamp": _last_ping,
        "raw": output,
    }


async def is_mac_online(max_age_s: float = 60.0) -> bool:
    """Fast Mac reachability check for preflight gating.

    Uses the cached `_last_ping_ok` from the 60s watchdog when fresh. On
    cold start or stale cache (>max_age_s since last ping) issues a fresh
    relay-ping to avoid false-positive "Mac online" when the bot just
    restarted and the watchdog hasn't ticked yet.

    Returns True iff the most recent signal says the Mac is reachable.
    """
    if not DESKTOP_ENABLED:
        return False
    age = time.time() - _last_ping if _last_ping else float("inf")
    if age <= max_age_s:
        return _last_ping_ok
    # Cache stale — live-check. ~1-3s typical on Tailscale.
    result = await ping()
    return bool(result.get("online", False))


async def authenticate(totp_code: str) -> dict:
    """Send TOTP code to Mac for verification.

    The code is passed through to Mac-side auth.sh which verifies it
    against the local TOTP secret. Bot never sees the secret.

    Returns dict with: authenticated, ttl_seconds, error
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    # H3 fix: validate TOTP code format before passing to SSH
    if not _SAFE_TOTP_CODE.fullmatch(totp_code):
        return {"authenticated": False, "ttl_seconds": 0, "error": "INVALID_CODE_FORMAT"}

    ok, output = await execute(f"relay-auth {totp_code}", timeout=10.0)

    if ok and output.startswith("AUTH_OK:"):
        parts = output.split(":")
        ttl = int(parts[2]) if len(parts) > 2 else DESKTOP_SESSION_TIMEOUT
        # Cache auth status for prompts.py
        global _cached_authenticated, _cached_session_remaining, _cached_auth_time
        _cached_authenticated = True
        _cached_session_remaining = ttl
        _cached_auth_time = time.monotonic()
        # Update auth tracking so watchdog doesn't re-trigger transition
        import bot.relay.watchdog as _wd  # noqa: PLC0415
        _wd._mac_was_authenticated = True
        # Immediately respawn any dead/missing relay daemons (same as startup)
        from bot.relay.watchdog import _respawn_unhealthy_daemons  # noqa: PLC0415
        asyncio.create_task(_respawn_unhealthy_daemons("post_auth"))
        # M1 fix: don't return session token — bot doesn't need it
        return {
            "authenticated": True,
            "ttl_seconds": ttl,
            "error": None,
        }
    else:
        return {
            "authenticated": False,
            "ttl_seconds": 0,
            "error": output,
        }


async def status() -> dict:
    """Get Mac relay status including auth session and relay daemons.

    Returns dict with: online, authenticated, session_remaining, linked_sessions
    """
    from bot.relay.registry import _load_registry  # noqa: PLC0415

    ping_result = await ping()
    if not ping_result["online"]:
        return {
            "online": False,
            "latency_ms": None,
            "authenticated": False,
            "session_remaining": 0,
            "idle_seconds": 0,
            "relay_sessions": [],
            "other_sessions": [],
            "error": ping_result.get("raw", "offline"),
        }

    # Check auth + sessions via relay-status
    from bot.relay.ssh import execute  # noqa: PLC0415
    ok, output = await execute("relay-status", timeout=10.0)

    if not ok:
        return {
            "online": True,
            "latency_ms": ping_result["latency_ms"],
            "authenticated": False,
            "session_remaining": 0,
            "idle_seconds": 0,
            "relay_sessions": [],
            "other_sessions": [],
            "error": output,
        }

    # Parse relay-status output (format: SESSION_VALID:{remaining}s remaining:idle_{idle}s)
    lines = output.strip().split("\n")
    authenticated = False
    session_remaining = 0
    idle_seconds = 0
    relay_sessions: list[str] = []
    other_sessions: list[str] = []
    current_section = ""

    for line in lines:
        if line.startswith("SESSION_VALID:"):
            authenticated = True
            try:
                session_remaining = int(line.split(":")[1].replace("s remaining", ""))
            except (ValueError, IndexError):
                session_remaining = 0
            # Extract idle time
            try:
                idle_part = [p for p in line.split(":") if p.startswith("idle_")]
                if idle_part:
                    idle_seconds = int(idle_part[0].replace("idle_", "").replace("s", ""))
            except (ValueError, IndexError):
                idle_seconds = 0
        elif line.startswith("NO_TMUX_SESSIONS"):
            pass
        elif line.startswith("RELAY_SESSIONS:"):
            current_section = "relay"
        elif line.startswith("OTHER_SESSIONS:"):
            current_section = "other"
        elif line.strip() and not line.startswith("AUTH_"):
            if current_section == "relay":
                relay_sessions.append(line.strip())
            elif current_section == "other":
                other_sessions.append(line.strip())
            else:
                # Fallback for backward compat
                other_sessions.append(line.strip())

    # Cross-reference with linked sessions registry
    links = _load_registry()
    linked_info: list[dict] = []
    for cid, entry in links.items():
        sname = entry.get("session_name", "")
        alive = any(sname in rs for rs in relay_sessions)
        linked_info.append({
            "chat_id": cid,
            "session_name": sname,
            "project_path": entry.get("project_path", ""),
            "session_alive": alive,
            "last_used": entry.get("last_used", ""),
        })

    # Cache auth status for get_cached_status() (used by prompts.py)
    global _cached_authenticated, _cached_session_remaining, _cached_auth_time
    _cached_authenticated = authenticated
    _cached_session_remaining = session_remaining
    _cached_auth_time = time.monotonic()

    return {
        "online": True,
        "latency_ms": ping_result["latency_ms"],
        "authenticated": authenticated,
        "session_remaining": session_remaining,
        "idle_seconds": idle_seconds,
        "relay_sessions": relay_sessions,
        "other_sessions": other_sessions,
        "linked_sessions": linked_info,
        "error": None,
    }


async def lock() -> dict:
    """Immediately revoke the Mac-side auth session and kill relay sessions.

    Revokes the auth token and kills relay daemons on the Mac.

    Returns dict with: locked, message
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    ok, output = await execute("relay-lock", timeout=10.0)
    return {
        "locked": ok and "LOCKED:" in output,
        "message": output,
    }


async def eager_respawn_daemon(chat_id: str) -> dict:
    """Spawn a fresh persistent daemon for a relay chat, detached.

    Called after any intentional daemon cycle (e.g. /mac-set-account rebind,
    token rotation, manual respawn) to keep the Mac->TG realtime tail alive
    without waiting for the user's next message.

    Reads uuid + cwd from the session registry. Skips silently if:
      - chat is not in registry
      - uuid or project_path missing
      - employee workstation control disabled

    Actual spawn is fire-and-forget via asyncio.create_task — bounded by
    _eager_respawn_one's internal timeout (20s ready + 10s acquire).
    Returns {ok, reason} for caller observability; a False `ok` is
    informational only, never fatal.

    This is the public companion of the watchdog's _eager_respawn_after_kick
    helper — shares _eager_respawn_one as the spawn primitive so behavior
    stays consistent across entry points.
    """
    if not DESKTOP_ENABLED:
        return {"ok": False, "reason": "disabled"}
    from bot.relay.registry import get_linked_session, _uuid_from_entry  # noqa: PLC0415
    from bot.relay.watchdog import _eager_respawn_one  # noqa: PLC0415

    try:
        entry = await get_linked_session(chat_id)
    except Exception as e:
        log.warning("eager_respawn_daemon: registry read failed chat=%s: %s", chat_id, e)
        return {"ok": False, "reason": "registry_read_failed"}
    if not entry:
        return {"ok": False, "reason": "no_linked_session"}
    uuid = _uuid_from_entry(entry)
    cwd = entry.get("project_path") or ""
    if not uuid or not cwd:
        return {"ok": False, "reason": "missing_uuid_or_cwd"}
    try:
        from bot.relay.pool import get_pool
    except Exception as e:
        log.warning("eager_respawn_daemon: pool import failed: %s", e)
        return {"ok": False, "reason": "pool_unavailable"}
    pool = get_pool()
    asyncio.create_task(_eager_respawn_one(chat_id, uuid, cwd, pool))
    log.info(
        "eager_respawn_daemon: detached spawn dispatched chat=%s uuid=%s",
        chat_id, uuid[:8],
    )
    return {"ok": True, "reason": "dispatched"}


async def update_pinned_jsonl(chat_id: str, uuid: str) -> dict:
    """Re-pin a relay chat's JSONL binding to a new session UUID.

    Builds /Users/<DESKTOP_USER>/.claude/projects/<encoded>/<uuid>.jsonl from
    the chat's stored project_path. Resets last_synced_offset to 0 so the
    next daemon invocation starts fresh.

    Returns {"success": bool, "new_path": str} or {"success": False, "error": str}.
    """
    from bot.relay.registry import (  # noqa: PLC0415
        _UUID_BARE_RE, _registry_lock, _load_registry, _save_registry,
        _encode_project_path,
    )

    if not _UUID_BARE_RE.fullmatch(uuid):
        return {"success": False, "error": f"Invalid UUID format: {uuid}"}
    async with _registry_lock:
        registry = _load_registry()
        entry = registry.get(str(chat_id))
        if not entry:
            return {"success": False, "error": "No relay binding for this chat"}
        project_path = entry.get("project_path", "")
        encoded = _encode_project_path(project_path)
        if not encoded:
            return {"success": False, "error": "No project_path in registry"}
        new_path = (
            f"/Users/{DESKTOP_USER}/.claude/projects/{encoded}/{uuid}.jsonl"
        )
        entry["pinned_jsonl_path"] = new_path
        entry["last_synced_offset"] = 0
        _save_registry(registry)
    log.info("Relay pinned_jsonl updated: chat=%s uuid=%s", chat_id, uuid)
    return {"success": True, "new_path": new_path}


async def install_mac_skills() -> dict:
    """Tar up scripts/desktop-relay/mac-skills/ and ship over SSH to Mac.

    Mac-side ``relay-install-skills`` validates the payload and writes to a
    pending dir (``~/.relay/pending-installs/skills/``).  The operator must
    run ``~/.relay/approve-install.sh`` on Mac to promote to live.

    Returns: {ok: bool, pending_approval: bool, output: str, ...}
    - ok=True  → legacy guard.sh (direct install, backward compat)
    - ok=False, pending_approval=True → pending Mac approval (FB-40)
    - ok=False, pending_approval=False → real error
    """
    import base64 as _b64
    from bot.relay.ssh import _ssh_cmd_base  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return {"ok": False, "pending_approval": False, "error": "DESKTOP_RELAY_DISABLED"}

    src_dir = Path("/app/scripts/desktop-relay/mac-skills")
    if not src_dir.is_dir():
        src_dir = Path("/host-repo/scripts/desktop-relay/mac-skills")
    if not src_dir.is_dir():
        return {"ok": False, "pending_approval": False,
                "error": f"mac_skills_dir_not_found:{src_dir}"}

    # Tar up the dir contents (not the dir itself — arcnames relative to src_dir)
    buf = io.BytesIO()
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for p in ("skills", "agents", "CLAUDE.md.snippet"):
                src_path = src_dir / p
                if src_path.exists():
                    tar.add(str(src_path), arcname=p)
    except Exception as e:
        return {"ok": False, "pending_approval": False, "error": f"tar_failed:{e}"}

    b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
    log.info("install_mac_skills: tar_size=%d b64_size=%d", len(buf.getvalue()), len(b64))

    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), "relay-install-skills",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(input=b64.encode("ascii")), timeout=60.0,
        )
        out = (out_b or b"").decode("utf-8", errors="replace").strip()
        err = (err_b or b"").decode("utf-8", errors="replace").strip()
        rc = proc.returncode
        # Detect pending approval (FB-40 guard.sh)
        if out.startswith("PENDING_APPROVAL:"):
            sha = _parse_install_sha(out)
            log.info("install_mac_skills: pending approval sha256=%s", sha[:12] if sha else "?")
            return {"ok": False, "pending_approval": True,
                    "output": out, "sha256": sha, "type": "skills"}
        # Legacy direct-install response (old guard.sh)
        ok = rc == 0 and out.startswith("OK:")
        log.info("install_mac_skills: rc=%s out=%.200s err=%.200s", rc, out, err)
        return {"ok": ok, "pending_approval": False,
                "output": out, "stderr": err, "rc": rc}
    except asyncio.TimeoutError:
        return {"ok": False, "pending_approval": False, "error": "ssh_timeout_60s"}
    except Exception as e:
        log.error("install_mac_skills failed: %s", e, exc_info=True)
        return {"ok": False, "pending_approval": False, "error": f"ssh_exception:{e}"}


async def install_mac_daemon() -> dict:
    """Sync scripts/desktop-relay/sdk_persistent_daemon.py to Mac.

    Mac-side ``relay-install-daemon`` validates the payload and writes to a
    pending dir (``~/.relay/pending-installs/daemon/``).  The operator must
    run ``~/.relay/approve-install.sh`` on Mac to promote to live.

    The MCP ``install_daemon`` action handles post-approval daemon respawn
    via ``check_install_status()`` — callers should call that after approval.

    Returns: {ok: bool, pending_approval: bool, output, size, sha256, ...}
    """
    import base64 as _b64
    import hashlib
    from bot.relay.ssh import _ssh_cmd_base  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return {"ok": False, "pending_approval": False, "error": "DESKTOP_RELAY_DISABLED"}

    src = Path("/app/scripts/desktop-relay/sdk_persistent_daemon.py")
    if not src.is_file():
        src = Path("/host-repo/scripts/desktop-relay/sdk_persistent_daemon.py")
    if not src.is_file():
        return {"ok": False, "pending_approval": False,
                "error": f"daemon_src_not_found:{src}"}

    content = src.read_bytes()
    size = len(content)
    if size == 0:
        return {"ok": False, "pending_approval": False, "error": "daemon_empty"}
    # Cap matches guard.sh validator — refuse oversized BEFORE SSH to save RTT
    if size > 2 * 1024 * 1024:
        return {"ok": False, "pending_approval": False,
                "error": f"daemon_too_large:{size}"}
    # Shebang sanity (mirrors guard.sh check; fail fast on repo-side)
    first_nl = content.find(b"\n")
    first_line = content[:first_nl] if first_nl > 0 else content
    if not (first_line.startswith(b"#!") and b"python" in first_line):
        return {"ok": False, "pending_approval": False, "error": "daemon_shebang_missing"}
    sha = hashlib.sha256(content).hexdigest()
    b64 = _b64.b64encode(content).decode("ascii")
    log.info("install_mac_daemon: size=%d sha256=%s", size, sha[:12])

    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), "relay-install-daemon",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(input=b64.encode("ascii")), timeout=30.0,
        )
        out = (out_b or b"").decode("utf-8", errors="replace").strip()
        err = (err_b or b"").decode("utf-8", errors="replace").strip()
        rc = proc.returncode
        # Detect pending approval (FB-40 guard.sh)
        if out.startswith("PENDING_APPROVAL:"):
            sha_from_mac = _parse_install_sha(out)
            log.info("install_mac_daemon: pending approval sha256=%s",
                     sha_from_mac[:12] if sha_from_mac else "?")
            return {"ok": False, "pending_approval": True,
                    "output": out, "sha256": sha, "type": "daemon",
                    "size": size}
        # Legacy direct-install response (old guard.sh)
        ok = rc == 0 and out.startswith("OK:")
        log.info("install_mac_daemon: rc=%s out=%.200s err=%.200s", rc, out, err)
        return {"ok": ok, "pending_approval": False,
                "output": out, "stderr": err, "rc": rc,
                "size": size, "sha256": sha}
    except asyncio.TimeoutError:
        return {"ok": False, "pending_approval": False, "error": "ssh_timeout_30s"}
    except Exception as e:
        log.error("install_mac_daemon failed: %s", e, exc_info=True)
        return {"ok": False, "pending_approval": False,
                "error": f"ssh_exception:{e}"}


def _parse_install_sha(output: str) -> str:
    """Extract sha256 from PENDING_APPROVAL:type:sha256=... response."""
    for part in output.split(":"):
        if part.startswith("sha256="):
            return part[7:]
    return ""


async def check_install_status() -> dict:
    """Check pending install status on Mac via relay-install-status.

    Returns: {ok: bool, pending: list[dict], raw: str}
    Each pending item: {type, sha256, age}
    If guard.sh doesn't support relay-install-status (old version),
    returns {ok: False, error: "not_supported"}.
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return {"ok": False, "error": "DESKTOP_RELAY_DISABLED"}

    try:
        _ok, raw_output = await execute("relay-install-status")
    except Exception as e:
        log.error("check_install_status failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    raw = raw_output.strip()
    if "BLOCKED:" in raw or "command not whitelisted" in raw:
        return {"ok": False, "error": "not_supported", "raw": raw}

    pending: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("PENDING:"):
            parts = line.split(":")
            if len(parts) >= 4:
                pending.append({
                    "type": parts[1],
                    "sha256": parts[2],
                    "age": parts[3],
                })
    return {"ok": True, "pending": pending, "raw": raw}
