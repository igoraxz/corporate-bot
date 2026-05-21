#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""MCP response size guard — prevents Claude SDK crash from oversized tool responses.

The Claude SDK has a 1MB JSON buffer limit. External MCP tools (browser snapshot,
page HTML extraction) can return responses exceeding this, causing fatal session crashes.

This script sits between the SDK and the MCP server, truncating oversized responses.

Two modes:
  stdio wrap:  python3 mcp_size_guard.py wrap -- command arg1 arg2
  SSE bridge:  python3 mcp_size_guard.py sse http://127.0.0.1:3001

Environment:
  MCP_MAX_RESPONSE_BYTES  Max response size before truncation (default: 500000)
  MCP_TOOL_TIMEOUT_SECS   Per-tool-call timeout in seconds (default: 120)
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Stay well under SDK's 1MB (1048576) limit
MAX_BYTES = int(os.environ.get("MCP_MAX_RESPONSE_BYTES", "500000"))

# Per-tool-call timeout — kills hung browser navigations/snapshots
TOOL_TIMEOUT = int(os.environ.get("MCP_TOOL_TIMEOUT_SECS", "120"))

# Timeout for SSE endpoint discovery (seconds)
_SSE_ENDPOINT_TIMEOUT = 30

# Directory to save screenshots (same as Playwright's output dir)
_IMAGE_SAVE_DIR = Path(os.environ.get("MCP_IMAGE_SAVE_DIR", "data/tmp"))

# Prefix for saved screenshot filenames (distinguishes browser engine)
_IMAGE_FILE_PREFIX = os.environ.get("MCP_IMAGE_FILE_PREFIX", "screenshot")

# Default viewport/colorScheme for create_tab (Camoufox defaults to 1280x720 dark)
_DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}
_DEFAULT_COLOR_SCHEME = "light"


def _log(msg: str):
    """Log to stderr (stdout is the MCP transport)."""
    print(f"[mcp-size-guard] {msg}", file=sys.stderr, flush=True)


def _save_image_to_file(data_b64: str, mime_type: str = "image/png") -> str | None:
    """Decode base64 image and save to file. Returns the file path or None on error."""
    ext = "png" if "png" in mime_type else "jpg" if "jpeg" in mime_type or "jpg" in mime_type else "png"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
    filename = f"{_IMAGE_FILE_PREFIX}-{ts}.{ext}"
    filepath = _IMAGE_SAVE_DIR / filename
    try:
        _IMAGE_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        raw_bytes = base64.b64decode(data_b64)
        filepath.write_bytes(raw_bytes)
        _log(f"Saved screenshot to {filepath} ({len(raw_bytes):,} bytes)")
        return str(filepath)
    except Exception as e:
        _log(f"Failed to save screenshot: {e}")
        return None


def _inject_create_tab_defaults(line: bytes) -> bytes:
    """Inject viewport/colorScheme defaults into create_tab MCP requests.

    Camoufox defaults to 1280x720 dark mode. This ensures all tabs get
    1920x1080 light mode even if the model forgets to pass these params.
    """
    if b'"create_tab"' not in line:
        return line
    try:
        msg = json.loads(line)
        method = msg.get("method", "")
        if method != "tools/call":
            return line
        params = msg.get("params", {})
        if params.get("name") != "create_tab":
            return line
        args = params.get("arguments", {})
        if "viewport" not in args:
            args["viewport"] = _DEFAULT_VIEWPORT
            _log("Injected default viewport 1920x1080 into create_tab")
        if "colorScheme" not in args:
            args["colorScheme"] = _DEFAULT_COLOR_SCHEME
            _log("Injected default colorScheme 'light' into create_tab")
        params["arguments"] = args
        msg["params"] = params
        return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    except (json.JSONDecodeError, ValueError, KeyError):
        return line


def _truncate_response(raw: str) -> str:
    """Truncate an oversized JSON-RPC response, preserving JSON structure.

    Finds content[].text fields (MCP tool response format) and truncates them.
    Falls back to hard byte-safe truncation if JSON parsing fails.
    Size comparisons use byte length (UTF-8) to match the SDK's buffer limit.
    """
    raw_bytes = len(raw.encode("utf-8", errors="replace"))
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Hard truncation — ensure valid UTF-8 boundary
        truncated = raw.encode("utf-8")[:MAX_BYTES].decode("utf-8", errors="ignore")
        _log(f"Hard truncated (unparseable JSON): {raw_bytes:,} → {MAX_BYTES:,} bytes")
        return truncated

    # Find content fields in the result and truncate oversized ones
    result = msg.get("result") if isinstance(msg, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for i, item in enumerate(content):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type == "text":
                text = item.get("text", "")
                text_bytes = len(text.encode("utf-8", errors="replace"))
                if text_bytes > MAX_BYTES // 2:
                    cutoff_bytes = MAX_BYTES // 3
                    truncated_text = text.encode("utf-8")[:cutoff_bytes].decode("utf-8", errors="ignore")
                    item["text"] = (
                        truncated_text
                        + f"\n\n... [TRUNCATED by size guard: response was {raw_bytes:,} bytes, "
                        f"exceeded {MAX_BYTES:,} byte limit. "
                        "Use browser_evaluate with a CSS selector for specific elements, "
                        "or screenshot for visual overview of large pages.]"
                    )
            elif item_type == "image":
                # Always save images to files (matches Playwright behavior).
                # Inline base64 wastes context window and risks SDK buffer overflow.
                data = item.get("data", "")
                if data:
                    saved_path = _save_image_to_file(data, item.get("mimeType", "image/png"))
                    if saved_path:
                        content[i] = {
                            "type": "text",
                            "text": f"[Screenshot saved to file]({saved_path})",
                        }

    # Re-encode and verify final size (json.dumps handles escaping safely)
    out = json.dumps(msg, ensure_ascii=False)
    out_bytes = len(out.encode("utf-8", errors="replace"))
    _log(f"Truncated: {raw_bytes:,} → {out_bytes:,} bytes")

    # Safety check: if still too large after truncation, hard-truncate the JSON string
    if out_bytes > MAX_BYTES:
        out = out.encode("utf-8")[:MAX_BYTES].decode("utf-8", errors="ignore")
        _log(f"Re-truncated to {MAX_BYTES:,} bytes (safety cap)")
    return out


def _maybe_truncate(line_bytes: bytes) -> bytes:
    """Process a JSON-RPC line: save images to files, truncate oversized text.

    Images are ALWAYS saved to files (matching Playwright behavior).
    Text content is only truncated when oversized.
    """
    text = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
    # Fast path: check if this line even contains image data
    has_image = b'"type":"image"' in line_bytes or b'"type": "image"' in line_bytes
    is_oversized = len(line_bytes) > MAX_BYTES
    if not has_image and not is_oversized:
        return line_bytes
    processed = _truncate_response(text)
    return (processed + "\n").encode("utf-8")


# ── stdio wrap mode ──────────────────────────────────────────────


async def _run_stdio_wrap(cmd: list[str]):
    """Wrap a stdio MCP subprocess, truncating oversized responses.

    Adds per-request timeout: if no stdout line arrives within TOOL_TIMEOUT seconds
    after a request was forwarded, emits a JSON-RPC timeout error response.
    """
    _log(f"Wrapping: {' '.join(cmd)} (timeout={TOOL_TIMEOUT}s)")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )
    # Track pending request IDs for timeout error responses
    _pending_ids: list[int | str | None] = []

    async def pipe_stdin():
        """Forward SDK stdin → subprocess stdin, tracking request IDs."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    if proc.stdin:
                        proc.stdin.close()
                    break
                # Track request ID for timeout error generation
                try:
                    msg = json.loads(line)
                    if "id" in msg:
                        _pending_ids.append(msg["id"])
                except (json.JSONDecodeError, ValueError):
                    pass
                line = _inject_create_tab_defaults(line)
                proc.stdin.write(line)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def pipe_stdout():
        """Forward subprocess stdout → SDK stdout (with truncation + timeout)."""
        buf = b""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=TOOL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # MCP tool hung — generate error response for pending request
                    req_id = _pending_ids.pop(0) if _pending_ids else None
                    _log(f"TIMEOUT after {TOOL_TIMEOUT}s waiting for response (req_id={req_id})")
                    error_resp = json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32000,
                            "message": f"Tool timed out after {TOOL_TIMEOUT}s. "
                                       "The page may be too heavy or unresponsive. "
                                       "Try: 1) screenshot instead of snapshot, "
                                       "2) a simpler URL, 3) increase timeout param.",
                        },
                    })
                    sys.stdout.buffer.write((error_resp + "\n").encode("utf-8"))
                    sys.stdout.buffer.flush()
                    continue
                if not chunk:
                    # EOF — flush remaining buffer
                    if buf:
                        buf = _maybe_truncate(buf)
                        sys.stdout.buffer.write(buf)
                        sys.stdout.buffer.flush()
                    break
                buf += chunk
                # Process complete lines (newline-delimited JSON-RPC)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line + b"\n"
                    # Clear matching pending ID on response
                    try:
                        resp = json.loads(line)
                        resp_id = resp.get("id")
                        if resp_id is not None and resp_id in _pending_ids:
                            _pending_ids.remove(resp_id)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    line = _maybe_truncate(line)
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def pipe_stderr():
        """Forward subprocess stderr → our stderr."""
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    tasks = [
        asyncio.create_task(pipe_stdin()),
        asyncio.create_task(pipe_stdout()),
        asyncio.create_task(pipe_stderr()),
    ]
    await proc.wait()
    # Cancel pipe tasks and wait for them to finish cleanly
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    sys.exit(proc.returncode or 0)


# ── SSE bridge mode ──────────────────────────────────────────────


def _validate_endpoint_url(base_url: str, endpoint_data: str) -> str | None:
    """Validate the SSE endpoint URL is safe (same host as base).

    Returns the full endpoint URL if valid, None otherwise.
    Prevents a compromised SSE server from redirecting requests to an attacker.
    """
    candidate = base_url + endpoint_data
    parsed_base = urlparse(base_url)
    parsed_candidate = urlparse(candidate)
    # Endpoint must be on the same host as the base URL
    if parsed_candidate.hostname != parsed_base.hostname:
        _log(f"Rejected endpoint URL (host mismatch): {candidate}")
        return None
    # Endpoint must use same scheme
    if parsed_candidate.scheme != parsed_base.scheme:
        _log(f"Rejected endpoint URL (scheme mismatch): {candidate}")
        return None
    return candidate


async def _run_sse_bridge(base_url: str):
    """Bridge an SSE MCP server to stdio, with response truncation.

    Connects to the SSE endpoint as a client, exposes stdio to the SDK.
    This preserves the shared browser model — each bridge instance gets its
    own session/tab on the shared Playwright SSE server.
    """
    import httpx

    _log(f"Bridging SSE: {base_url}")
    endpoint_url: str | None = None
    endpoint_ready = asyncio.Event()
    bridge_failed = False

    # POST requests use TOOL_TIMEOUT for read; SSE stream uses no read timeout
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=float(TOOL_TIMEOUT), write=30, pool=None),
    )

    async def read_sse():
        """Read SSE events from upstream, forward as JSON-RPC on stdout."""
        nonlocal endpoint_url, bridge_failed
        try:
            # SSE stream must stay open indefinitely — override read timeout to None
            sse_timeout = httpx.Timeout(connect=30, read=None, write=30, pool=None)
            async with client.stream("GET", f"{base_url}/sse", timeout=sse_timeout) as resp:
                event_type = ""
                data_lines: list[str] = []
                async for raw_line in resp.aiter_lines():
                    if raw_line.startswith("event:"):
                        event_type = raw_line[6:].strip()
                    elif raw_line.startswith("data:"):
                        data_lines.append(raw_line[5:].strip())
                    elif raw_line == "":
                        # End of SSE event
                        data = "\n".join(data_lines)
                        data_lines = []
                        if not event_type:
                            continue  # Skip malformed events without type
                        if event_type == "endpoint":
                            validated = _validate_endpoint_url(base_url, data)
                            if validated:
                                endpoint_url = validated
                                endpoint_ready.set()
                                _log(f"Endpoint: {endpoint_url}")
                            else:
                                _log("Endpoint URL validation failed — bridge aborting")
                                bridge_failed = True
                                endpoint_ready.set()  # Unblock forward_stdin so it can exit
                                return
                        elif event_type == "message":
                            # JSON-RPC response — process images + truncate if needed
                            data_bytes = data.encode("utf-8", errors="replace")
                            has_image = b'"type":"image"' in data_bytes or b'"type": "image"' in data_bytes
                            if has_image or len(data_bytes) > MAX_BYTES:
                                data = _truncate_response(data)
                            sys.stdout.write(data + "\n")
                            sys.stdout.flush()
                        event_type = ""
        except Exception as e:
            _log(f"SSE read error: {type(e).__name__}: {e}")
            bridge_failed = True
            endpoint_ready.set()  # Unblock forward_stdin on SSE failure

    async def forward_stdin():
        """Read JSON-RPC from SDK stdin, POST to SSE endpoint."""
        try:
            await asyncio.wait_for(endpoint_ready.wait(), timeout=_SSE_ENDPOINT_TIMEOUT)
        except asyncio.TimeoutError:
            _log(f"Endpoint not received within {_SSE_ENDPOINT_TIMEOUT}s — bridge aborting")
            return
        if bridge_failed or endpoint_url is None:
            _log("Bridge failed — not forwarding stdin")
            return
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if endpoint_url is None:
                    _log("Endpoint URL lost — skipping request")
                    continue
                await client.post(
                    endpoint_url,
                    content=line,
                    headers={"Content-Type": "application/json"},
                )
        except (BrokenPipeError, ConnectionResetError) as e:
            _log(f"stdin pipe closed: {e}")
        except Exception as e:
            _log(f"stdin forward error: {type(e).__name__}: {e}")

    try:
        await asyncio.gather(read_sse(), forward_stdin())
    finally:
        await client.aclose()


# ── main ─────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  mcp_size_guard.py wrap -- command [args...]\n"
            "  mcp_size_guard.py sse <base_url>",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "wrap":
        try:
            sep_idx = sys.argv.index("--")
        except ValueError:
            print("Error: wrap mode requires -- separator before command", file=sys.stderr)
            sys.exit(1)
        cmd = sys.argv[sep_idx + 1:]
        if not cmd:
            print("Error: no command specified after --", file=sys.stderr)
            sys.exit(1)
        asyncio.run(_run_stdio_wrap(cmd))
    elif mode == "sse":
        if len(sys.argv) < 3:
            print("Error: sse mode requires base URL", file=sys.stderr)
            sys.exit(1)
        base_url = sys.argv[2].rstrip("/")
        asyncio.run(_run_sse_bridge(base_url))
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
