# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee Mac OAuth token management for workstation control.

Handles OAuth token promotion, employee Mac account enumeration, per-channel
account binding, token push to employee Mac, and the remote Claude CLI OAuth
reauth flow (setup-token v7).
"""

import asyncio
import re
import logging

log = logging.getLogger(__name__)

__all__ = [
    "promote_oauth_token",
    "list_mac_accounts",
    "set_chat_account",
    "push_oauth_to_mac",
    "start_mac_oauth_flow",
    "submit_mac_oauth_code",
    "poll_mac_oauth_status",
    "kill_mac_oauth_flow",
    "_MAC_OAUTH_LAUNCH_TIMEOUT_S",
    "_MAC_OAUTH_PASTE_TIMEOUT_S",
    "_MAC_OAUTH_FINALIZE_TIMEOUT_S",
]

import time

_MAC_OAUTH_LAUNCH_TIMEOUT_S = 75.0  # SSH call returns within ~60s when URL captured
_MAC_OAUTH_PASTE_TIMEOUT_S = 15.0   # paste-back SSH is tiny, should be fast
_MAC_OAUTH_FINALIZE_TIMEOUT_S = 90.0  # setup-token prints token within seconds


async def promote_oauth_token(label: str) -> dict:
    """Rename the default ~/.relay/oauth_token to oauth_token.<label> on Mac.

    Called after a successful /mac-reauth flow so the freshly-written token
    moves into the label slot. Idempotent at the file-rename level; if the
    default slot is empty (already promoted) returns an error from guard.sh.
    """
    from bot.relay.registry import validate_account_label  # noqa: PLC0415
    from bot.relay.ssh import execute  # noqa: PLC0415

    if not validate_account_label(label):
        return {"success": False, "error": "invalid_label"}
    ok, output = await execute(f"relay-promote-token {label}", timeout=15.0)
    if not ok:
        return {"success": False, "error": output, "raw": output}
    if output.strip().startswith("PROMOTED:"):
        return {"success": True, "label": label, "raw": output.strip()}
    return {"success": False, "error": output.strip(), "raw": output.strip()}


async def list_mac_accounts() -> dict:
    """Enumerate account labels present on Mac (no token values)."""
    from bot.relay.ssh import execute  # noqa: PLC0415

    ok, output = await execute("relay-list-accounts", timeout=10.0)
    if not ok:
        return {"success": False, "error": output, "labels": []}
    raw = output.strip()
    if raw.startswith("LABELS:"):
        labels = [s for s in raw[len("LABELS:"):].strip().split() if s]
        return {"success": True, "labels": labels}
    if raw == "NO_LABELS":
        return {"success": True, "labels": []}
    return {"success": False, "error": raw, "labels": []}


async def set_chat_account(chat_id: str, label: str) -> dict:
    """Bind a relay chat to a named OAuth account.

    Stored as account_label on the session registry entry. Read at daemon
    spawn time by RelayPersistentClient and passed as the 4th arg of
    relay-sdk-persistent so guard.sh exports the right token file.
    """
    from bot.relay.registry import (  # noqa: PLC0415
        validate_account_label, _registry_lock, _load_registry, _save_registry,
    )

    if not validate_account_label(label):
        return {"success": False, "error": "invalid_label"}
    async with _registry_lock:
        registry = _load_registry()
        key = str(chat_id)
        if key not in registry:
            return {"success": False, "error": "no_linked_session"}
        registry[key]["account_label"] = label
        _save_registry(registry)
    log.info("Session account_label set: chat_id=%s label=%s", chat_id, label)
    return {"success": True, "chat_id": key, "account_label": label}


async def push_oauth_to_mac(label: str, token: str) -> dict:
    """Push an OAuth token to Mac relay as oauth_token.<label>.

    Uses relay-push-token SSH command (guard.sh): token sent as base64 on stdin,
    written atomically to ~/.relay/oauth_token.<label> with 0600 perms.
    This unifies bot-side and Mac-side OAuth profiles — one add_oauth_profile()
    call syncs to both locations.
    """
    from bot.relay.registry import validate_account_label  # noqa: PLC0415
    from bot.relay.ssh import _ssh_cmd_base  # noqa: PLC0415

    if not validate_account_label(label):
        return {"success": False, "error": "invalid_label"}
    if not token or not token.startswith("sk-ant-"):
        return {"success": False, "error": "invalid_token_format"}
    import base64 as _b64
    token_b64 = _b64.b64encode(token.encode("utf-8"))
    cmd = f"relay-push-token {label}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=token_b64), timeout=15.0,
        )
        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 and output.startswith("PUSHED:"):
            log.info("push_oauth_to_mac: label=%s synced OK", label)
            return {"success": True, "label": label}
        log.warning("push_oauth_to_mac failed: rc=%s out=%s err=%s",
                    proc.returncode, output[:200], stderr.decode()[:200])
        return {"success": False, "error": output or "unknown"}
    except asyncio.TimeoutError:
        return {"success": False, "error": "timeout"}
    except Exception as e:
        log.error("push_oauth_to_mac error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def start_mac_oauth_flow() -> tuple[bool, str]:
    """Trigger remote `claude setup-token` on Mac; return (ok, oauth_url_or_error).

    v7: no port, no SSH tunnel. Helper parses its state file first line as
    `URL|pid` — we only need the URL to post to the user. The pid is kept
    on the Mac side for helper lifecycle management via relay-claude-login-kill.

    Returns:
        (True, oauth_url)      on success
        (False, error_message) on failure (TOTP_EXPIRED, unparseable, etc.)
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415
    from bot.relay.ssh import _ssh_cmd_base, _clean_ssh_stderr  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return False, "DESKTOP_RELAY_DISABLED"

    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), "relay-claude-login",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "SSH_NOT_FOUND"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_MAC_OAUTH_LAUNCH_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"ssh_timeout_after_{_MAC_OAUTH_LAUNCH_TIMEOUT_S:.0f}s"

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    rc = proc.returncode

    log.info(
        "Phase14c v7: relay-claude-login rc=%s stdout=%.300s stderr=%.200s",
        rc, stdout, stderr,
    )

    # Order matters: check stdout FIRST (authoritative source from Mac helper).
    # SSH-level noise (e.g. known_hosts warnings) often comes with rc=1 even
    # when the actual helper output is in stdout — so we prioritise stdout.

    first_line = stdout.splitlines()[0] if stdout else ""

    # Mac helper signalled an error explicitly
    if first_line.startswith("ERROR:"):
        return False, first_line

    # Success path: URL|pid (v7) or legacy URL|PORT|PID (v6, port ignored)
    if first_line.startswith("https://"):
        parts = first_line.split("|")
        oauth_url = parts[0]
        from urllib.parse import urlparse as _up14c
        _p = _up14c(oauth_url)
        # setup-token emits claude.com/cai/oauth/authorize URLs; /login and
        # console flows use the other two. All three Anthropic-owned hosts.
        if _p.hostname not in ("claude.com", "claude.ai", "console.anthropic.com"):
            return False, f"untrusted_url_host:{_p.hostname}"
        return True, oauth_url

    # Neither ERROR: nor URL found in stdout → SSH / guard.sh failure.
    # Filter out the harmless known_hosts write-permission warning from
    # stderr so the message surfaces the actual problem.
    stderr_clean = _clean_ssh_stderr(stderr)

    # TOTP hints
    if ("auth" in stderr_clean.lower()
            or "session" in stdout.lower()
            or "TOTP" in stderr_clean):
        return False, "TOTP_EXPIRED"

    # Direct, self-describing error
    msg_parts = [f"ssh_rc={rc}"]
    if stdout:
        msg_parts.append(f"stdout={stdout[:300]}")
    if stderr_clean:
        msg_parts.append(f"stderr={stderr_clean[:300]}")
    return False, " | ".join(msg_parts)


async def submit_mac_oauth_code(code: str) -> tuple[bool, str]:
    """Pipe OAuth code to Mac helper via `relay-claude-login-paste` (stdin).

    Guard.sh validates the code (strict alphabet, length cap) and writes
    ~/.relay/claude-login.code (0600). Helper polls that file and types the
    code into setup-token's stdin. Code travels via SSH stdin, never argv,
    so there's no shell quoting / history / process-listing leak.

    Returns (success, server_response_or_error).
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415
    from bot.relay.ssh import _ssh_cmd_base, _clean_ssh_stderr  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return False, "DESKTOP_RELAY_DISABLED"

    # Belt-and-braces client-side validation — guard.sh re-validates.
    if not re.fullmatch(r"[A-Za-z0-9_#\-]+", code):
        return False, "invalid_code_format"
    if not (1 <= len(code) <= 512):
        return False, f"invalid_code_length:{len(code)}"

    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), "relay-claude-login-paste",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "SSH_NOT_FOUND"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=code.encode("utf-8") + b"\n"),
            timeout=_MAC_OAUTH_PASTE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"ssh_timeout_after_{_MAC_OAUTH_PASTE_TIMEOUT_S:.0f}s"

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    rc = proc.returncode

    # Never log the code itself — log only length + response status.
    log.info(
        "Phase14c v7: relay-claude-login-paste rc=%s code_len=%d stdout=%.200s stderr=%.200s",
        rc, len(code), stdout, stderr,
    )

    if rc == 0 and stdout == "CODE_ACCEPTED":
        return True, "CODE_ACCEPTED"
    # Surface authoritative helper output first, then filtered stderr
    stderr_clean = _clean_ssh_stderr(stderr)
    msg_parts = [f"rc={rc}"]
    if stdout:
        msg_parts.append(f"stdout={stdout[:300]}")
    if stderr_clean:
        msg_parts.append(f"stderr={stderr_clean[:300]}")
    return False, " | ".join(msg_parts)


async def poll_mac_oauth_status(timeout_s: float = _MAC_OAUTH_FINALIZE_TIMEOUT_S) -> tuple[bool, str]:
    """Poll Mac helper state file until setup-token finishes.

    Terminal states (line 2 of ~/.relay/claude-login.state):
      TOKEN_SAVED                  → success, token stored on Mac
      ERROR:<reason>               → helper error during capture/write
      TIMEOUT                      → helper timed out waiting for code
      EXITED:status=N              → claude setup-token exited unexpectedly

    Returns (success, terminal_state_or_error).
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415
    from bot.relay.ssh import _ssh_cmd_base  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return False, "DESKTOP_RELAY_DISABLED"
    deadline = time.monotonic() + timeout_s
    last_state = "waiting"
    while time.monotonic() < deadline:
        try:
            proc = await asyncio.create_subprocess_exec(
                *_ssh_cmd_base(), "relay-claude-login-status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except (FileNotFoundError, asyncio.TimeoutError):
            await asyncio.sleep(2.0)
            continue
        except Exception as _e:
            log.warning("poll_mac_oauth_status SSH error: %s", _e)
            await asyncio.sleep(2.0)
            continue
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        # State file: line 1 = URL|pid, line 2 = terminal status (when set)
        lines = stdout.splitlines()
        if len(lines) >= 2:
            terminal = lines[1].strip()
            last_state = terminal
            if terminal == "TOKEN_SAVED":
                return True, "TOKEN_SAVED"
            if terminal.startswith(("ERROR:", "TIMEOUT", "EXITED:")):
                return False, terminal
        await asyncio.sleep(2.0)
    return False, f"poll_timeout_after_{timeout_s:.0f}s (last_state={last_state})"


async def kill_mac_oauth_flow() -> str:
    """Abort an in-flight Mac /login helper (cleanup). Returns status string."""
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415
    from bot.relay.ssh import _ssh_cmd_base  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return "DESKTOP_RELAY_DISABLED"
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), "relay-claude-login-kill",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        return stdout_b.decode("utf-8", errors="replace").strip() or "UNKNOWN"
    except Exception as e:
        return f"ERROR:{e}"
