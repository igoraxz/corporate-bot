# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""SSH transport layer for employee workstation control.

Handles all SSH communication with employee Mac workstations: command execution,
file fetch/push, and SSH process tracking. All commands are gated by Mac-side guard.sh.
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "execute",
    "fetch_file",
    "push_file",
    "_ssh_cmd_base",
    "_clean_ssh_stderr",
    "_SSH_CONFIG_DIR",
    "_ssh_procs",
    "_PUSH_FILE_EXTS",
    "_PUSH_FILE_MAX_BYTES",
    "_PUSH_FILE_TIMEOUT_S",
    "_SSH_NOISE_PREFIXES",
]

# SSH command template — uses the ssh-config Host alias set up in entrypoint.
# HostName is resolved dynamically via bot.relay.mac_resolver (multi-tier fallback
# chain: DESKTOP_HOST_IP env pin → Tailscale API → DNS → disk cache →
# hardcoded default). Cached IP is refreshed by mac_health_watchdog on each
# 60s tick. Call `_ssh_cmd_base()` (NOT the old `_SSH_CMD_BASE` constant)
# at every SSH spawn site so the latest resolved IP is used.
_SSH_CONFIG_DIR = os.path.expanduser("~/.ssh-config")

# SSH process tracking — track spawned SSH subprocesses for orphan monitoring
_ssh_procs: dict[int, tuple[float, str]] = {}  # pid → (spawn_time, description)

# Extension allowlist — kept in sync with relay-push-file case in guard.sh.
# Covers media types Mac Claude/Gemini can describe: images, video, audio, docs.
_PUSH_FILE_EXTS = frozenset({
    "jpg", "jpeg", "png", "gif", "webp", "heic",
    "mp4", "mov", "webm",
    "pdf",
    "ogg", "m4a", "mp3",
    "txt", "md", "json", "html", "css", "js",
    "docx", "xlsx", "csv", "pptx",
    "zip", "7z", "tar", "gz", "rar",
})

# 50MB — mirrors guard.sh RELAY_FETCH cap and relay-push-file cap.
_PUSH_FILE_MAX_BYTES = 52_428_800

# SSH+stdin upload for a 50MB file takes ~10-60s depending on link.
_PUSH_FILE_TIMEOUT_S = 180.0

# SSH stderr noise lines that are harmless and should be stripped from
# user-facing error messages. The known_hosts line appears on every
# connection because our config uses a custom UserKnownHostsFile path but
# SSH also tries the default ~/.ssh/known_hosts (non-writable) as a fallback.
_SSH_NOISE_PREFIXES = (
    "Failed to add the host to the list of known hosts",
    "Warning: Permanently added",
)


def _ssh_cmd_base() -> list[str]:
    """Build the SSH command prefix using the currently-resolved Mac IP.

    Returns a fresh list each call — the HostName value reflects whatever
    bot.relay.mac_resolver has most recently cached. Sync + non-blocking.
    """
    # Local import to avoid circular-dep risk at module load.
    from bot.relay.mac_resolver import get_ssh_host_opts  # noqa: PLC0415
    from bot.relay.core import DESKTOP_USER, DESKTOP_HOST  # noqa: PLC0415
    return [
        "/usr/bin/ssh",
        "-F", f"{_SSH_CONFIG_DIR}/config",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        *get_ssh_host_opts(),
        f"{DESKTOP_USER}@{DESKTOP_HOST}",
    ]


def _clean_ssh_stderr(stderr: str) -> str:
    """Strip harmless SSH warnings so error messages surface the real cause."""
    if not stderr:
        return ""
    kept = [
        line for line in stderr.splitlines()
        if line.strip() and not any(line.startswith(p) for p in _SSH_NOISE_PREFIXES)
    ]
    return "\n".join(kept).strip()


async def execute(cmd: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Execute a command on the Mac via SSH.

    Returns (success, output). The command is wrapped by guard.sh on the Mac
    side, which validates the TOTP session before allowing execution.

    Args:
        cmd: The command string (will be passed as SSH_ORIGINAL_COMMAND)
        timeout: Timeout in seconds (default 15s)

    Returns:
        Tuple of (success: bool, output: str)
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return False, "DESKTOP_RELAY_DISABLED"

    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Track SSH subprocess for orphan monitoring
        _ssh_procs[proc.pid] = (time.time(), cmd[:80])
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _ssh_procs.pop(proc.pid, None)
            return False, "TIMEOUT"

        _ssh_procs.pop(proc.pid, None)
        output = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            return True, output
        else:
            # guard.sh returns non-zero for AUTH_REQUIRED, BLOCKED, etc.
            # Clean SSH noise (known_hosts warnings) from the error output
            # so user sees only the actual error, not infra noise.
            clean_err = _clean_ssh_stderr(err)
            return False, output or clean_err

    except FileNotFoundError:
        return False, "SSH_NOT_FOUND"
    except Exception as e:
        log.error("Workstation relay execute error: %s", e, exc_info=True)
        return False, f"ERROR:{e}"


async def fetch_file(file_path: str, timeout: float = 60.0) -> tuple[bool, bytes]:
    """Fetch a binary file from Mac via SSH relay-fetch command.

    Uses guard.sh relay-fetch which outputs base64-encoded file content.
    Decodes on the server side for binary-safe transfer.

    Args:
        file_path: Absolute path to file on Mac
        timeout: Timeout in seconds (default 60s for large files)

    Returns:
        Tuple of (success: bool, file_bytes: bytes). On failure, file_bytes
        contains an error message encoded as UTF-8.
    """
    # Minimal path validation — absolute path, no shell metachars, no nulls.
    # Full path traversal defence lives on the Mac side in relay-fetch itself.
    if not file_path or not file_path.startswith("/"):
        return False, b"INVALID_PATH:must_be_absolute"
    if any(ch in file_path for ch in ("\x00", "\n", "\r", ";", "&", "|",
                                       "`", "$", "<", ">", "*", "?", "\\")):
        return False, b"INVALID_PATH:shell_metachar"

    ok, output = await execute(f"relay-fetch {file_path}", timeout=timeout)
    if not ok:
        return False, (output or "FETCH_FAILED").encode()

    try:
        file_bytes = base64.b64decode(output)
        log.info("relay-fetch: path=%s size=%d bytes", file_path, len(file_bytes))
        return True, file_bytes
    except Exception as e:
        log.warning("relay-fetch base64 decode failed: %s", e)
        return False, f"BASE64_DECODE_ERROR:{e}".encode()


async def push_file(local_path: str,
                    timeout: float = _PUSH_FILE_TIMEOUT_S) -> tuple[bool, str]:
    """Push a local file to the Mac's relay inbox via SSH relay-push-file.

    Reads the file, computes sha256, base64-encodes the bytes, and streams them
    over SSH stdin to guard.sh. Guard decodes them to ~/.relay/inbox/<sha>.<ext>
    and echoes the absolute Mac path. Mac Claude can then Read()/describe the
    file natively at that path.

    Args:
        local_path: Absolute path to the local file on the bot container.
        timeout: SSH timeout in seconds (default 180s — covers 50MB upload).

    Returns:
        (success, mac_abs_path_or_error_message)
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return False, "DESKTOP_RELAY_DISABLED"

    # --- Bot-side validation (belt-and-braces; guard.sh re-validates) ---
    try:
        p = Path(local_path).resolve()
    except (OSError, ValueError) as e:
        return False, f"INVALID_PATH:{e}"
    if not p.is_file():
        return False, "INVALID_PATH:not_a_file"

    try:
        fsize = p.stat().st_size
    except OSError as e:
        return False, f"STAT_ERROR:{e}"
    if fsize == 0:
        return False, "INVALID_PATH:empty_file"
    if fsize > _PUSH_FILE_MAX_BYTES:
        return False, f"FILE_TOO_LARGE:{fsize}_bytes"

    ext = p.suffix.lstrip(".").lower()
    if ext not in _PUSH_FILE_EXTS:
        return False, f"EXT_NOT_ALLOWED:{ext or '<none>'}"

    # --- Read, hash, encode ---
    try:
        file_bytes = p.read_bytes()
    except OSError as e:
        return False, f"READ_ERROR:{e}"
    sha = hashlib.sha256(file_bytes).hexdigest()
    encoded = base64.b64encode(file_bytes)

    # --- SSH upload with stdin (mirrors submit_mac_oauth_code pattern) ---
    cmd = f"relay-push-file {sha} {ext}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *_ssh_cmd_base(), cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "SSH_NOT_FOUND"
    except Exception as e:
        log.error("relay-push-file spawn error: %s", e, exc_info=True)
        return False, f"SPAWN_ERROR:{e}"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=encoded),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"TIMEOUT_AFTER_{int(timeout)}s"

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()

    if proc.returncode == 0 and stdout.startswith("/"):
        log.info("relay-push-file: sha=%s.. ext=%s size=%d -> %s",
                 sha[:12], ext, fsize, stdout)
        return True, stdout

    log.warning("relay-push-file failed rc=%s stdout=%.200s stderr=%.200s "
                "sha=%s.. ext=%s size=%d",
                proc.returncode, stdout, stderr, sha[:12], ext, fsize)
    return False, stdout or stderr or f"PUSH_FAILED:rc={proc.returncode}"
