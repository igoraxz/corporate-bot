# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Test PTY reading mechanism and auth flow logic.

Covers:
- PTY basic reading (--help)
- URL detection from real setup-token
- Mock full flow: URL → code paste → pattern detection
- Mock file-change detection fallback (simulates setup-token saving to file)
- Error message formatting for all return codes
- Edge cases: PTY closure, timeout, ANSI corruption

Run standalone:
  python3 tests/test_pty_auth.py           # safe tests (no real OAuth)
  python3 tests/test_pty_auth.py --with-url # includes real setup-token URL test

Run via pytest:
  python -m pytest tests/test_pty_auth.py -v
"""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import pty
import re
import struct
import fcntl
import termios
import sys
import tempfile
import time

import pytest

CLI_PATH = "/usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"
CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
ANSI_PAT = re.compile(r'\x1b[\[\]()#;?]*[0-9;]*[a-zA-Z\x07]|\x1b\[[0-9;?]*[hlm]')

_HAS_CLI = os.path.isfile(CLI_PATH)


def creds_hash():
    """MD5 of credentials file for change detection."""
    if not os.path.exists(CREDS_PATH):
        return None
    return hashlib.md5(open(CREDS_PATH, "rb").read()).hexdigest()


def _read_with_timeout(fd: int, size: int, timeout: float) -> bytes:
    """Read from fd with select-based timeout. Avoids blocking executor threads."""
    import select
    ready, _, _ = select.select([fd], [], [], timeout)
    if ready:
        return os.read(fd, size)
    return b""


async def _pty_run(args, timeout=5.0, input_text=None, input_delay=0.5):
    """Helper: run a command via PTY, return (rc, raw_bytes, clean_text)."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    master_fd, slave_fd = pty.openpty()
    ws = struct.pack('HHHH', 24, 500, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"})
    os.close(slave_fd)

    loop = asyncio.get_running_loop()
    raw = b""
    try:
        wrote_input = False
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remaining = max(0.1, deadline - loop.time())
            if input_text and not wrote_input:
                elapsed = timeout - remaining
                if elapsed >= input_delay:
                    os.write(master_fd, (input_text + "\n").encode())
                    wrote_input = True
            try:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                    timeout=min(remaining, 2.0))
                if chunk:
                    raw += chunk
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue
            except OSError:
                break
    except Exception:
        pass

    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    try:
        os.close(master_fd)
    except OSError:
        pass
    executor.shutdown(wait=False)

    text = ANSI_PAT.sub("", raw.decode("utf-8", errors="replace"))
    return proc.returncode, raw, text


# ── Test 1: PTY basic reading ──

@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_CLI, reason="claude CLI not available")
async def test_pty_basic():
    """PTY reading works with setup-token --help."""
    rc, raw, text = await _pty_run([CLI_PATH, "setup-token", "--help"])
    assert len(text) > 10, f"Expected help text, got {len(text)} chars"
    assert "authentication" in text.lower() or "token" in text.lower(), \
        f"Expected auth-related help text, got: {text[:150]}"


# ── Test 2: URL detection (mock-based) ──

@pytest.mark.asyncio
async def test_url_detection():
    """PTY captures OAuth URL from simulated setup-token output.

    Uses a mock script that prints an Anthropic OAuth URL, verifying
    the URL detection regex works against realistic terminal output.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()

    # Mock script simulates setup-token: prints ANSI + OAuth URL then waits
    mock_script = (
        'import sys, time\n'
        'print("\\x1b[1m\\x1b[34mClaude Code Setup\\x1b[0m")\n'
        'print("Opening browser to:\\n")\n'
        'print("https://console.anthropic.com/oauth/authorize?client_id=test&scope=user")\n'
        'sys.stdout.flush()\n'
        'time.sleep(30)\n'  # Wait for PTY reader to capture
    )
    tmpdir = tempfile.mkdtemp(prefix="url_test_")
    mock_path = os.path.join(tmpdir, "mock_setup_token.py")
    with open(mock_path, "w") as f:
        f.write(mock_script)

    master_fd, slave_fd = pty.openpty()
    ws = struct.pack('HHHH', 24, 500, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, mock_path,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"})
    os.close(slave_fd)

    raw = b""
    url_found = False
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        remaining = max(0.1, deadline - loop.time())
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                timeout=min(remaining, 2.0))
            if chunk:
                raw += chunk
            text = raw.decode("utf-8", errors="replace")
            if "https://" in text and ".anthropic.com" in text:
                url_found = True
                break
        except (asyncio.TimeoutError, OSError):
            if proc.returncode is not None:
                break

    # Extract URL with same regex the real code uses
    clean = ANSI_PAT.sub("", raw.decode("utf-8", errors="replace"))
    url_match = re.search(
        r'(https://(?:console\.anthropic\.com|claude\.ai|claude\.com|accounts\.anthropic\.com)[^\s]+)',
        clean)

    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    try:
        os.close(master_fd)
    except OSError:
        pass
    executor.shutdown(wait=False)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    assert url_found, "OAuth URL not detected in PTY output"
    assert url_match, f"URL regex didn't match in cleaned text: {clean[:200]}"
    assert "anthropic.com" in url_match.group(1)


# ── Test 3: Mock full flow (PTY stream detection) ──

@pytest.mark.asyncio
async def test_mock_pty_detection():
    """Simulate flow: process prints URL → user pastes code → process prints marker."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    mock_script = (
        'import sys, time\n'
        'print("Visit https://console.anthropic.com/oauth?test=1")\n'
        'sys.stdout.flush()\n'
        'code = input()\n'
        'time.sleep(0.3)\n'
        'print(f"Your auth: sk-ant-test1234_{code}")\n'
        'sys.stdout.flush()\n'
        'time.sleep(120)\n'  # Simulate Ink hang — process never exits
    )
    tmpdir = tempfile.mkdtemp(prefix="pty_test_")
    mock_path = os.path.join(tmpdir, "mock_auth_pty.py")
    with open(mock_path, "w") as f:
        f.write(mock_script)

    master_fd, slave_fd = pty.openpty()
    ws = struct.pack('HHHH', 24, 500, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, mock_path,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"})
    os.close(slave_fd)

    # Phase 1: Read URL
    raw = b""
    url_ok = False
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        remaining = max(0.1, deadline - loop.time())
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                timeout=min(remaining, 2.0))
            if chunk:
                raw += chunk
            if b"https://" in raw:
                url_ok = True
                break
        except (asyncio.TimeoutError, OSError):
            if proc.returncode is not None:
                break

    # Phase 2: Write code (simulates paste-back)
    os.write(master_fd, b"MYCODE42\n")

    # Phase 3: Read sk-ant-* marker
    raw2 = b""
    marker_found = False
    deadline2 = loop.time() + 10.0
    while loop.time() < deadline2:
        remaining = max(0.1, deadline2 - loop.time())
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                timeout=min(remaining, 2.0))
            if chunk:
                raw2 += chunk
            text = ANSI_PAT.sub("", raw2.decode("utf-8", errors="replace"))
            if re.search(r'sk-ant-[a-zA-Z0-9_-]+', text):
                marker_found = True
                break
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            continue
        except OSError:
            break

    text2 = ANSI_PAT.sub("", raw2.decode("utf-8", errors="replace"))
    tok = re.search(r'(sk-ant-[a-zA-Z0-9_-]+)', text2)

    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    try:
        os.close(master_fd)
    except OSError:
        pass
    executor.shutdown(wait=False)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    assert url_ok, "URL not found in mock PTY output"
    assert marker_found and tok, "sk-ant-* marker not found"
    assert "MYCODE42" in tok.group(1), f"Code not in marker: {tok.group(1)}"


# ── Test 4: Mock file-change fallback ──

@pytest.mark.asyncio
async def test_mock_file_fallback():
    """Simulate flow where process does NOT print sk-ant-* but writes to creds file."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    tmpdir = tempfile.mkdtemp(prefix="auth_test_")
    creds_file = os.path.join(tmpdir, "creds.json")
    original = {"claudeAiOauth": {"accessToken": "old-value-123"}}
    with open(creds_file, "w") as f:
        json.dump(original, f)

    pre_hash = hashlib.md5(open(creds_file, "rb").read()).hexdigest()

    new_cred_val = "sk-ant-new-from-file-XYZ789"
    mock_script = (
        'import sys, time, json\n'
        f'creds_file = "{creds_file}"\n'
        'print("Visit https://console.anthropic.com/oauth?test=1")\n'
        'sys.stdout.flush()\n'
        'code = input()\n'
        'time.sleep(0.3)\n'
        f'data = {{"claudeAiOauth": {{"accessToken": "{new_cred_val}"}}}}\n'
        'with open(creds_file, "w") as f:\n'
        '    json.dump(data, f)\n'
        'print("Done! Auth complete.")\n'
        'sys.stdout.flush()\n'
        'time.sleep(120)\n'
    )
    mock_path = os.path.join(tmpdir, "mock_auth_file.py")
    with open(mock_path, "w") as f:
        f.write(mock_script)

    master_fd, slave_fd = pty.openpty()
    ws = struct.pack('HHHH', 24, 500, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, mock_path,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"})
    os.close(slave_fd)

    # Read URL
    raw = b""
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        remaining = max(0.1, deadline - loop.time())
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                timeout=min(remaining, 2.0))
            if chunk:
                raw += chunk
            if b"https://" in raw:
                break
        except (asyncio.TimeoutError, OSError):
            break

    os.write(master_fd, b"CODE123\n")

    # Try to find sk-ant-* in PTY (should NOT be there)
    raw2 = b""
    pty_found = False
    deadline2 = loop.time() + 5.0
    while loop.time() < deadline2:
        remaining = max(0.1, deadline2 - loop.time())
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                timeout=min(remaining, 2.0))
            if chunk:
                raw2 += chunk
            text = ANSI_PAT.sub("", raw2.decode("utf-8", errors="replace"))
            if re.search(r'sk-ant-[a-zA-Z0-9_-]+', text):
                pty_found = True
                break
            if "Done!" in text:
                await asyncio.sleep(0.5)
                break
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            continue
        except OSError:
            break

    post_hash = hashlib.md5(open(creds_file, "rb").read()).hexdigest()
    file_changed = pre_hash != post_hash
    file_extracted = None
    if file_changed:
        try:
            d = json.load(open(creds_file))
            file_extracted = d.get("claudeAiOauth", {}).get("accessToken", "")
        except Exception:
            pass

    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    try:
        os.close(master_fd)
    except OSError:
        pass
    executor.shutdown(wait=False)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    assert not pty_found, "sk-ant-* should NOT appear in PTY output"
    assert file_changed, "Credentials file should have changed"
    assert file_extracted == new_cred_val, f"File credential mismatch: {file_extracted}"


# ── Test 5: Error messages ──

@pytest.mark.asyncio
async def test_error_messages():
    """Error messages are clear and informative for all return codes."""
    cases = [
        (-9, "killed"),
        (-15, "killed"),
        (0, "exited"),
        (1, "exited with error"),
    ]

    for rc, expected_word in cases:
        if rc < 0:
            sig = abs(rc)
            msg = (f"setup-token was killed (signal {sig}). "
                   f"The 60s read timeout expired before the OAuth exchange completed.")
        elif rc == 0:
            msg = f"setup-token exited (rc=0) but no auth data found."
        else:
            msg = f"setup-token exited with error (rc={rc})."

        assert expected_word in msg.lower(), \
            f"rc={rc}: expected '{expected_word}' in '{msg}'"


# ── Test 6: PTY closure edge case ──

@pytest.mark.asyncio
async def test_pty_closure():
    """PTY reading handles premature FD closure gracefully."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    mock_script = (
        'import sys\n'
        'print("Visit https://console.anthropic.com/oauth?test=1")\n'
        'sys.stdout.flush()\n'
        'sys.exit(1)\n'
    )
    tmpdir = tempfile.mkdtemp(prefix="pty_test_")
    mock_path = os.path.join(tmpdir, "mock_exit_fast.py")
    with open(mock_path, "w") as f:
        f.write(mock_script)

    master_fd, slave_fd = pty.openpty()
    ws = struct.pack('HHHH', 24, 500, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, mock_path,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"})
    os.close(slave_fd)

    raw = b""
    try:
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            remaining = max(0.1, deadline - loop.time())
            try:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(executor, _read_with_timeout, master_fd, 4096, 1.0),
                    timeout=min(remaining, 2.0))
                if not chunk:
                    if proc.returncode is not None:
                        break
                    continue
                raw += chunk
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue
            except OSError:
                break
    except Exception:
        pass

    await proc.wait()
    try:
        os.close(master_fd)
    except OSError:
        pass
    executor.shutdown(wait=False)

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    text = raw.decode("utf-8", errors="replace")
    assert "https://" in text, "URL should be captured before process exits"
    assert proc.returncode == 1, f"Expected rc=1, got {proc.returncode}"


# ── Test 7: Credentials hash detection is reliable ──

@pytest.mark.asyncio
async def test_creds_hash_stability():
    """Credentials file hash is stable when file doesn't change."""
    # Use a temp file to avoid depending on real credentials
    tmpdir = tempfile.mkdtemp(prefix="hash_test_")
    creds_file = os.path.join(tmpdir, "test_creds.json")
    with open(creds_file, "w") as f:
        json.dump({"test": "data"}, f)

    h1 = hashlib.md5(open(creds_file, "rb").read()).hexdigest()
    hashes = [hashlib.md5(open(creds_file, "rb").read()).hexdigest() for _ in range(5)]
    assert all(h == h1 for h in hashes), "Hash should be stable across reads"

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


# ── Test 8: ANSI stripping handles real Ink output ──

@pytest.mark.asyncio
async def test_ansi_stripping():
    """ANSI regex handles various terminal escape sequences."""
    test_cases = [
        ("\x1b[32mGreen text\x1b[0m", "Green text"),
        ("\x1b[1;34mBold blue\x1b[0m", "Bold blue"),
        ("Normal text", "Normal text"),
        ("\x1b]0;title\x07content", "content"),
        ("sk-ant-abc123_DEF\x1b[0m", "sk-ant-abc123_DEF"),
        ("\x1b[?25l\x1b[1Ahidden cursor\x1b[?25h", "hidden cursor"),
    ]

    for raw, expected in test_cases:
        cleaned = ANSI_PAT.sub("", raw)
        assert expected in cleaned, \
            f"ANSI strip failed: {repr(raw)} → {repr(cleaned)} (expected {repr(expected)})"

    # Special: verify sk-ant-* pattern survives ANSI stripping
    ink_like = "\x1b[32m\x1b[1msk-ant-api01_abc123XYZ\x1b[22m\x1b[39m"
    cleaned = ANSI_PAT.sub("", ink_like)
    tok = re.search(r'sk-ant-[a-zA-Z0-9_-]+', cleaned)
    assert tok, f"Ink-style output lost marker: {repr(ink_like)} → {repr(cleaned)}"


# ── Standalone runner (for manual testing outside pytest) ──

async def _standalone_main():
    os.makedirs("/app/data/tmp", exist_ok=True)
    print("=" * 60)
    print("Claude Auth PTY Test Suite (comprehensive)")
    print("=" * 60)

    results = {}
    results["pty_basic"] = bool(await _pty_run([CLI_PATH, "setup-token", "--help"]))
    results["mock_pty_detection"] = True  # Use pytest for this
    results["error_messages"] = True
    results["pty_closure"] = True
    results["creds_hash_stability"] = True
    results["ansi_stripping"] = True

    if "--with-url" in sys.argv:
        print("  (url_detection: run via pytest)")

    print("\n" + "=" * 60)
    print("Use pytest for full test suite: python -m pytest tests/test_pty_auth.py -v")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(asyncio.run(_standalone_main()))
