#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Mac-side OAuth helper for `claude setup-token` (v7 — long-lived token).

Replaces the v1-v6 `/login` flow. Uses `claude setup-token`, Anthropic's
purpose-built headless-auth command, which prints a 1-year OAuth token
after the user completes PKCE with a hosted callback.

Flow:
  1. Spawn `claude setup-token` in a wide PTY (NO_BROWSER suppresses
     Safari auto-open, TIOCSWINSZ sets cols=500 so URL doesn't wrap).
  2. Capture OAuth URL from stdout (reconstructed across any wrapping).
  3. Write URL to ~/.relay/claude-login.state (line 1: `URL|pid`).
  4. Poll ~/.relay/claude-login.code every 0.5s; when bot delivers it,
     type code + Enter into setup-token's stdin.
  5. Capture the printed `sk-ant-oat...` token from stdout.
  6. Write token to ~/.relay/oauth_token (0600) atomically.
  7. Append `TOKEN_SAVED` to state file; exit.

The bot never sees the token — guard.sh's `relay-sdk-query` exports
CLAUDE_CODE_OAUTH_TOKEN from the file before spawning claude for inference.

State file format:
    Line 1 (URL ready):    <oauth_url>|<pid>
    Line 2 (terminal):     TOKEN_SAVED | TIMEOUT | ERROR:<reason> | EXITED:<status>

Log file: ~/.relay/claude-login.log
"""
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time


HOME = os.path.expanduser("~")
RELAY_DIR = os.path.join(HOME, ".relay")
STATE_FILE = os.path.join(RELAY_DIR, "claude-login.state")
CODE_FILE = os.path.join(RELAY_DIR, "claude-login.code")
TOKEN_FILE = os.path.join(RELAY_DIR, "oauth_token")
LOG_FILE = os.path.join(RELAY_DIR, "claude-login.log")
CLAUDE_BIN_CANDIDATES = [
    "/Users/youruser/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "claude",  # PATH lookup fallback
]

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# setup-token prints a long-lived OAuth token. Observed format:
# `sk-ant-oat01-<long base64-url-safe body>`. Match generously but strict enough
# to avoid false positives in log decoration.
TOKEN_RE = re.compile(r"sk-ant-oat\d+-[A-Za-z0-9_\-]{40,}")
# Valid OAuth URL prefixes from Anthropic. `claude.com/cai/oauth/` is what
# `claude setup-token` actually emits (v2.1.x). The other two kept for
# forward/backward compat with `/login` and console flows.
OAUTH_URL_PREFIXES = (
    "https://claude.com/cai/oauth/",
    "https://claude.ai/",
    "https://console.anthropic.com/",
)

URL_CAPTURE_TIMEOUT_S = 60   # setup-token usually prints URL in <5s
CODE_WAIT_TIMEOUT_S = 300    # 5 min for user to OAuth and paste code
TOKEN_CAPTURE_TIMEOUT_S = 60 # wait after code submission for token print


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except Exception:
        pass


def find_claude_bin() -> str | None:
    for path in CLAUDE_BIN_CANDIDATES:
        if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    import shutil
    return shutil.which("claude")


def write_state(line: str, append: bool = False) -> None:
    mode = "a" if append else "w"
    try:
        with open(STATE_FILE, mode) as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as e:
        log(f"write_state error: {e}")


def extract_url(buf: bytes) -> str | None:
    """Reconstruct OAuth URL from PTY buffer — tolerant of ANSI + line wrap.

    claude setup-token can render the URL wrapped across lines with ANSI
    colour codes (e.g. when terminal width is small). We:
      1. Decode UTF-8 and strip all ANSI CSI escapes.
      2. Locate an OAuth URL prefix.
      3. Walk forward collecting URL-safe chars, skipping CR/LF (continuations).
      4. Stop at first whitespace or non-URL char.
    Returns the reconstructed URL or None if nothing found / too short.
    """
    text = buf.decode("utf-8", errors="replace")
    clean = ANSI_RE.sub("", text)
    start = -1
    for p in OAUTH_URL_PREFIXES:
        idx = clean.find(p)
        if idx >= 0:
            start = idx
            break
    if start < 0:
        return None
    # URL-safe char set per RFC 3986 (generous — covers all OAuth URL chars).
    safe = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        "-._~:/?#[]@!$&'()*+,;=%"
    )
    out = []
    for ch in clean[start:]:
        if ch in " \t":
            break
        if ch in "\r\n":
            continue  # skip line wraps within URL
        if ch in safe:
            out.append(ch)
        else:
            break
    url = "".join(out).rstrip(".,;:)")
    # Sanity: PKCE OAuth URL must include `state=` parameter
    if len(url) < 40 or "state=" not in url:
        return None
    return url


def atomic_write(path: str, content: str, mode: int = 0o600) -> None:
    """Write content to path atomically with the given file mode."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.rename(tmp, path)


def kill_child(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def main() -> int:
    os.makedirs(RELAY_DIR, exist_ok=True)
    log("=== claude_login_helper (setup-token v7) start ===")

    claude_bin = find_claude_bin()
    if not claude_bin:
        log("ERROR: claude binary not found")
        write_state("ERROR:claude_binary_not_found")
        return 1
    log(f"Using claude binary: {claude_bin}")

    # CWD into ~/.relay — bot-controlled folder, avoids random-dir trust prompts.
    try:
        os.chdir(RELAY_DIR)
        log(f"CWD set to {RELAY_DIR}")
    except OSError as e:
        log(f"chdir failed (non-fatal): {e}")

    # Pre-clean stale code file so a leftover doesn't trigger immediate submit.
    try:
        os.remove(CODE_FILE)
    except FileNotFoundError:
        pass

    pid, fd = pty.fork()
    if pid == 0:
        # child process — exec claude setup-token
        os.environ["TERM"] = "xterm-256color"
        # Suppress Safari auto-open (multiple env vars because different CLI
        # libs respect different ones).
        os.environ["NO_BROWSER"] = "1"
        os.environ["CLAUDE_NO_BROWSER"] = "1"
        os.environ["BROWSER"] = "/bin/echo"
        try:
            os.execv(claude_bin, [claude_bin, "setup-token"])
        except Exception as e:
            sys.stderr.write(f"exec failed: {e}\n")
            os._exit(1)

    # parent — set wide terminal so OAuth URL renders unwrapped.
    # COLUMNS env var alone is ignored by libs that read winsize via ioctl;
    # TIOCSWINSZ on the master fd is the authoritative path. Format: (rows,
    # cols, xpixel, ypixel). Cols=500 is well beyond any realistic URL length.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 500, 0, 0))
        log("PTY winsize set to 500x50")
    except OSError as e:
        log(f"TIOCSWINSZ failed (non-fatal, URL reconstruction still handles wrapping): {e}")

    log(f"Spawned claude setup-token pid={pid}")
    buf = b""
    found_url = None

    # === Phase 1: capture OAuth URL ===
    start_t = time.monotonic()
    while time.monotonic() - start_t < URL_CAPTURE_TIMEOUT_S:
        try:
            r, _, _ = select.select([fd], [], [], 0.5)
        except OSError:
            break
        if fd in r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                log("PTY closed unexpectedly during URL capture")
                break
            buf += chunk

        url = extract_url(buf)
        if url:
            found_url = url
            log(f"Captured URL (len={len(url)})")
            write_state(f"{found_url}|{pid}")
            break

    if not found_url:
        elapsed = time.monotonic() - start_t
        log(f"Failed to capture URL after {elapsed:.1f}s. Buf tail: {buf[-500:]!r}")
        write_state(f"ERROR:no_url_captured_after_{elapsed:.0f}s")
        kill_child(pid)
        return 1

    # === Phase 2: wait for bot to deliver the OAuth code via CODE_FILE ===
    log(f"URL posted — waiting up to {CODE_WAIT_TIMEOUT_S}s for code paste-back")
    sent_code = False
    code_start = time.monotonic()
    while time.monotonic() - code_start < CODE_WAIT_TIMEOUT_S:
        # Check if claude exited unexpectedly
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                log(f"claude exited unexpectedly during code wait, status={status}")
                write_state(f"EXITED:status={status}", append=True)
                return 1
        except ChildProcessError:
            log("claude process gone during code wait")
            write_state("ERROR:claude_gone_during_code_wait", append=True)
            return 1

        # Drain PTY (prevents buffer fill) — discard content until code sent
        try:
            r, _, _ = select.select([fd], [], [], 0.5)
            if fd in r:
                chunk = os.read(fd, 4096)
                if chunk:
                    buf += chunk
        except OSError:
            pass

        # Check for code file delivered by bot
        if not os.path.isfile(CODE_FILE):
            continue
        try:
            with open(CODE_FILE, "r") as f:
                code = f.read().strip()
        except OSError as e:
            log(f"read CODE_FILE failed: {e}")
            time.sleep(0.5)
            continue
        # Single-use: delete immediately
        try:
            os.remove(CODE_FILE)
        except OSError:
            pass
        if not code:
            log("empty code file — ignoring")
            continue
        # Defensive re-validation (guard.sh validates on write too)
        if not re.fullmatch(r"[A-Za-z0-9_\-#]+", code):
            log("code format invalid — ignoring")
            continue
        try:
            # Write code THEN newline-submit. setup-token's Ink TUI needs a
            # line-feed (\n) for "Enter" — plain \r displays as part of the
            # masked input but doesn't trigger submit. Write as two ops so
            # TTY line-discipline reliably sees an end-of-line. Small sleep
            # in between lets Ink render the code echo before submit arrives.
            os.write(fd, code.encode("utf-8"))
            time.sleep(0.3)
            os.write(fd, b"\r\n")
            sent_code = True
            log(f"Submitted code to stdin (len={len(code)})")
            break
        except OSError as e:
            log(f"write code failed: {e}")
            write_state(f"ERROR:code_write_failed:{e}", append=True)
            kill_child(pid)
            return 1

    if not sent_code:
        log(f"Timeout waiting for code after {CODE_WAIT_TIMEOUT_S}s")
        write_state("TIMEOUT", append=True)
        kill_child(pid)
        return 1

    # === Phase 3: capture printed token ===
    found_token = None
    tok_start = time.monotonic()
    while time.monotonic() - tok_start < TOKEN_CAPTURE_TIMEOUT_S:
        try:
            r, _, _ = select.select([fd], [], [], 0.5)
        except OSError:
            break
        if fd in r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                # EOF — setup-token exited. Check buf for token.
                break
            buf += chunk
        clean = ANSI_RE.sub("", buf.decode("utf-8", errors="replace"))
        m = TOKEN_RE.search(clean)
        if m:
            found_token = m.group(0)
            log(f"Captured token (len={len(found_token)})")
            break

    if not found_token:
        elapsed = time.monotonic() - tok_start
        log(f"Failed to capture token after {elapsed:.1f}s. Buf tail: {buf[-500:]!r}")
        write_state(f"ERROR:no_token_captured_after_{elapsed:.0f}s", append=True)
        kill_child(pid)
        return 1

    # === Phase 4: persist token to disk (0600, atomic) ===
    try:
        atomic_write(TOKEN_FILE, found_token + "\n", mode=0o600)
        log(f"Token written to {TOKEN_FILE}")
    except OSError as e:
        log(f"token write failed: {e}")
        write_state(f"ERROR:token_write_failed:{e}", append=True)
        kill_child(pid)
        return 1

    write_state("TOKEN_SAVED", append=True)
    kill_child(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
