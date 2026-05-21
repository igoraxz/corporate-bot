#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee Workstation Control — persistent SDK daemon, Mac-side long-lived Claude session.

Replaces the ephemeral sdk_daemon.py (subprocess-per-query). One daemon per workstation
channel, held open across queries. Pinned JSONL UUID is resumed by each claude
invocation — same shared-JSONL semantics as Desktop Claude.app (concurrent append).

Invocation contract (from guard.sh via SSH):
  sdk_persistent_daemon.py <chat_id> <uuid> <cwd_b64>

Protocol on stdin/stdout (line-delimited JSON, UTF-8):
  Bot → Daemon:
    {"type":"query","id":"<correlation>","text":"<query_text>"}
    {"type":"ping"}
    {"type":"quit"}
  Daemon → Bot:
    {"type":"ready","uuid":"...","chat_id":"...","pid":1234,"resumed":true}
    {"type":"pong","ts":1234567890}
    {"type":"stream","query_id":"<id>","entry":{<raw_stream_json>}}
    {"type":"result","query_id":"<id>","cost_usd":0.01,"turns":3,"status":"success"}
    {"type":"error","query_id":"<id>","error":"..."}
    {"type":"heartbeat","ts":1234567890,"state":"idle|busy"}   # every 30s
    {"type":"exit","reason":"..."}                               # on clean shutdown

Resource lifecycle (per chat_id sanitised):
  ~/.relay/daemons/<chat>.pid    — canonical PID file (atomic write, lock-protected)
  ~/.relay/daemons/<chat>.state  — JSON: status, uuid, started_at, last_heartbeat,
                                    queries_served, current_query_id, pid,
                                    model, effort
  ~/.relay/daemons/<chat>.log    — rotating 10MB x 3 per-daemon log

Orphan defence: on startup, checks for an existing pidfile for this chat:
  - If PID alive AND cmdline matches this script → exit with "already_running"
  - If PID dead OR mismatched → claim pidfile, log reaping event
  - If pidfile missing → create fresh

Security:
  - stdin/stdout is SSH-channelled; guard.sh validates the session per command
  - chat_id must match ^-?[0-9]+$ (sanitised); used only for filename
  - OAuth token loaded by parent shell (guard.sh exports CLAUDE_CODE_OAUTH_TOKEN)
  - No shell interpolation: all I/O is JSON on stdin/stdout pipes

Requires: `pip install claude-agent-sdk` on Mac (one-time).
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import re
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_HOME = Path.home()
_DAEMONS_DIR = _HOME / ".relay" / "daemons"
_PROJECTS_DIR = _HOME / ".claude" / "projects"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NEW_SESSION = "NEW"  # Sentinel: start fresh session (no --resume)
_CHAT_ID_RE = re.compile(r"^-?[0-9]+$")
_MAX_QUERY_BYTES = 64 * 1024  # 64KB per query text (same as ephemeral daemon)
_MAX_CWD_BYTES = 4 * 1024
_HEARTBEAT_INTERVAL_S = 30.0
_IDLE_TIMEOUT_S = 24 * 3600  # 24h — auto-exit if no query for a day
_QUIT_TIMEOUT_S = 10.0
_SUBPROCESS_IDLE_TIMEOUT_S = 300  # seconds of quiet before closing subprocess stdin
_SUBPROCESS_STDIN_CLOSE_KILL_S = 30  # force-kill proc if it lingers this long after stdin close

# Desktop-turn catchup (Option A: offset-delta on next query).
# Surfaces Desktop.app user/assistant text turns written between TG queries.
# See relay-chats skill for architecture.
# Sizing per spec: last 10 query-response pairs with minimal truncation +
# a summary preview for any older turns that fall outside the detail window.
_CATCHUP_DETAIL_PAIRS = 10         # last N query-response pairs shown in full
# Per-text cap effectively disabled — align with normal TG behaviour.
# TG delivery layer auto-splits >4096-char messages at paragraph/line/sentence
# boundaries (telegram.py:_split_message). We rely on that here too rather than
# truncating content. 50000 is a soft ceiling per single text block to protect
# against pathological 10MB JSONL entries; real safety is _CATCHUP_MAX_BYTES.
_CATCHUP_TRUNCATE_CHARS = 50000    # per-text soft cap (TG auto-split handles overflow)
_CATCHUP_SUMMARY_PREVIEW_CHARS = 160  # per-text preview in summary tail
_CATCHUP_MAX_BYTES = 10 * 1024 * 1024  # safety: never read >10MB of delta
# QA case 14 mitigation: first-attach reads from file-tail rather than head
# when the JSONL exceeds this window. Since we only surface the last
# `_CATCHUP_DETAIL_PAIRS` pairs anyway, reading the full prefix just burns
# I/O + CPU and risks pushing daemon init past the SSH timeout (~15s) for
# large (50MB+) JSONLs. 2 MB holds roughly 20-100 typical query-response
# pairs — generous buffer for the detail window while keeping init O(1).
# Delta mode (offset advances per emit) is naturally bounded by how much
# was appended since the last emit, so only first_run needs this cap.
_CATCHUP_FIRST_RUN_TAIL_BYTES = 2 * 1024 * 1024
# Reverse-scan settings — used by is_initial path to guarantee "exactly last
# N pairs" regardless of JSONL depth or tool-chatter density.
_CATCHUP_REV_CHUNK = 64 * 1024           # backward read block size
_CATCHUP_REV_SAFETY = 50 * 1024 * 1024   # hard cap on bytes scanned
_CATCHUP_MAX_LINE_BYTES = 1 * 1024 * 1024  # per-line DoS guard before json.loads

# Always-on realtime tailer (item 2). Polls the pinned JSONL every
# _REALTIME_TAIL_INTERVAL_S, dedups the daemon's own writes via message-id
# set + offset-range fallback, forwards Desktop-typed turns as `desktop_turn`
# events. See relay-chats skill for architecture + rationale.
_REALTIME_TAIL_INTERVAL_S = 0.5        # range: 0.1-5.0; 500ms = ≤2s user-visible latency
_REALTIME_DEDUP_CAP = 10_000            # message.id set bound (~400 KB per daemon at cap)
_REALTIME_OWN_RANGES_CAP = 100          # last N own-write byte ranges retained
_REALTIME_LINE_MAX_BYTES = 1_000_000    # skip pathological single lines
_REALTIME_SLICE_MAX_BYTES = 10 * 1024 * 1024  # per-tick slice-read cap (SEC-4)
_REALTIME_STARTUP_DELAY_S = 1.5         # let initial catchup fire first
_REALTIME_TIMING_LOG_MS = 100           # log slow reads / large batches

log: logging.Logger = logging.getLogger("sdk_persistent_daemon")


# ─────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────

def _emit(obj: dict) -> None:
    """Write one JSON line to stdout. Never raises."""
    try:
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _setup_logging(chat_id: str) -> None:
    """Per-daemon rotating file log. Also mirrors to stderr for ad-hoc debug."""
    log_path = _DAEMONS_DIR / f"{chat_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(log_path), maxBytes=10 * 1024 * 1024, backupCount=3)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                          datefmt="%Y-%m-%dT%H:%M:%SZ")
    )
    handler.setLevel(logging.DEBUG)
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    stderr.setLevel(logging.WARNING)
    log.addHandler(stderr)


# ─────────────────────────────────────────────────────────────────────────
# Desktop-turn catchup helpers (Option A: offset-delta on next query)
# ─────────────────────────────────────────────────────────────────────────

def _offset_path(chat_id: str) -> Path:
    return _DAEMONS_DIR / f"{chat_id}.offset"


def _load_offset(chat_id: str, uuid: str) -> int | None:
    """Return last-synced JSONL byte offset for this chat+uuid, or None.

    Offset file format: '<uuid>:<offset>'. If the stored uuid does not match
    the current session uuid (chat re-pinned to a new JSONL), returns None
    so the caller treats this as a first-attach / initial catchup.
    Legacy format (bare integer) is treated as mismatch → None, which is
    safe since legacy files were written without uuid binding.
    """
    try:
        raw = _offset_path(chat_id).read_text().strip()
    except OSError:
        return None
    if ":" not in raw:
        # Legacy bare-int file: no uuid binding, force re-initialisation.
        return None
    stored_uuid, _, off_str = raw.partition(":")
    if stored_uuid != uuid:
        return None
    try:
        return int(off_str)
    except ValueError:
        return None


def _save_offset(chat_id: str, uuid: str, offset: int) -> None:
    """Atomic write of current JSONL byte offset keyed by (chat_id, uuid)."""
    p = _offset_path(chat_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".offset.tmp")
        tmp.write_text(f"{uuid}:{max(0, int(offset))}")
        tmp.replace(p)
    except Exception as e:
        log.warning("save_offset chat=%s uuid=%s failed: %s", chat_id, uuid, e)


def _tail_offset_path(chat_id: str, uuid: str) -> Path:
    """Realtime-tailer offset file — separate from per-query `_offset_path`
    to avoid the two-writers race between the tailer and run_query's
    `_commit_offset_to_eof`. Keyed by (chat_id, uuid) directly in filename.
    """
    return _DAEMONS_DIR / f"{chat_id}.{uuid}.tail_offset"


def _load_tail_offset(chat_id: str, uuid: str) -> int:
    """Return last tailer-synced JSONL byte offset, or 0 if unset.

    Returns 0 (not None) since the tailer's "first tick" semantics are
    "catch up from current EOF" — not "replay backlog" (the existing
    `_emit_desktop_catchup` initial-attach branch handles backlog).
    """
    try:
        raw = _tail_offset_path(chat_id, uuid).read_text().strip()
        return max(0, int(raw))
    except (OSError, ValueError):
        return 0


def _save_tail_offset(chat_id: str, uuid: str, offset: int) -> None:
    """Atomic write of tailer offset. Never raises."""
    p = _tail_offset_path(chat_id, uuid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tail_offset.tmp")
        tmp.write_text(str(max(0, int(offset))))
        tmp.replace(p)
    except Exception as e:
        log.warning("save_tail_offset chat=%s uuid=%s failed: %s", chat_id, uuid, e)


def _find_jsonl(uuid: str) -> Path | None:
    """Locate ~/.claude/projects/*/<uuid>.jsonl. Returns None if not found.

    Claude CLI writes JSONL to ~/.claude/projects/<slugified-cwd>/<uuid>.jsonl.
    We don't need to know the slug — one match per UUID is guaranteed.
    """
    try:
        for proj in _PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            candidate = proj / f"{uuid}.jsonl"
            if candidate.is_file():
                return candidate
    except OSError:
        pass


def _find_newest_jsonl_for_cwd(cwd: str) -> tuple[str, str] | None:
    """Find the newest JSONL in the project dir for a given cwd.

    Claude CLI slugifies the cwd path: /Users/youruser/dev/test -> -Users-youruser-dev-myproject
    Returns (uuid, full_path) or None.
    """
    slug = cwd.replace("/", "-")
    proj_dir = _PROJECTS_DIR / slug
    if not proj_dir.is_dir():
        return None
    best: Path | None = None
    best_mtime = 0.0
    try:
        for f in proj_dir.iterdir():
            if f.suffix == ".jsonl" and _UUID_RE.match(f.stem):
                mt = f.stat().st_mtime
                if mt > best_mtime:
                    best_mtime = mt
                    best = f
    except OSError:
        return None
    if best is None:
        return None
    return best.stem, str(best)


def _detect_model_from_jsonl(uuid: str) -> str | None:
    """Read the JSONL tail and extract the model from the last assistant entry.

    Desktop.app may use a different model (e.g. Opus 4.7 with 1M context) than
    the CLI default. We auto-detect and pass --model to match, preventing
    context window mismatches that cause "Prompt is too long" errors.

    Returns the model string (e.g. "claude-opus-4-7") or None if not found.
    """
    jsonl = _find_jsonl(uuid)
    if not jsonl:
        return None
    try:
        # Read last 32KB — enough to find the most recent assistant entry
        size = jsonl.stat().st_size
        read_from = max(0, size - 32768)
        with open(jsonl, "rb") as f:
            if read_from > 0:
                f.seek(read_from)
            chunk = f.read().decode("utf-8", errors="replace")
        # Scan lines in reverse for the last assistant entry with a real model
        for line in reversed(chunk.split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "assistant":
                    model = obj.get("message", {}).get("model", "")
                    if model and model != "<synthetic>":
                        return model
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError as e:
        log.debug("_detect_model_from_jsonl error: %s", e)
    return None


def _detect_settings(cwd: str) -> dict[str, str | None]:
    """Read Claude settings from project/user config to report effort level.

    Reads ``effortLevel`` from settings.json (project-level first, then user-level).
    Values: "low", "medium", "high", "xhigh" (Claude Code schema).

    NOTE: the ``--effort`` CLI flag may override this at runtime; we can only
    detect the static settings.json value here.

    Returns dict with key ``effort``.  Value is the effort string or None if
    not configured in any settings file.
    """
    # Check project .claude/settings.json, then user ~/.claude/settings.json
    for settings_path in [
        Path(cwd) / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ]:
        try:
            data = json.loads(settings_path.read_text())
            if "effortLevel" in data:
                val = str(data["effortLevel"]).strip()
                # Sanitize for space-delimited protocol (no whitespace/specials)
                val = val.replace("\n", "").replace("\r", "").split()[0][:32]
                return {"effort": val}
        except Exception:
            continue
    return {"effort": None}


def _extract_tool_names(entry: dict) -> list[str]:
    """Extract tool_use names from a JSONL entry (assistant turns mostly).

    Returns empty list if entry is not a tool_use carrier. Preserves order.
    """
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") != "tool_use":
            continue
        name = c.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _extract_text(entry: dict) -> str:
    """Pull user-visible text from a JSONL entry. Returns '' if not applicable.

    Filters out:
    - `thinking` blocks (internal reasoning — not shown to users)
    - `tool_use` / `tool_result` blocks (noisy; not a conversational turn)
    Keeps only `type=text` content. Returns '' if the entry has no text
    (e.g. an assistant turn that only issued tool calls, or a user entry
    that is actually a tool_result payload).
    """
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") != "text":
                continue  # skip thinking, tool_use, tool_result, etc.
            t = c.get("text")
            if isinstance(t, str) and t:
                parts.append(t)
    elif isinstance(content, str):
        parts.append(content)
    return "\n".join(parts).strip()


# Extensions worth proxying from Desktop.app tool calls to TG.
# Source code (.py, .js, .go, etc.) is excluded — too noisy.
_PROXY_FILE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".heic",
    ".pdf", ".csv", ".xlsx", ".html",
})


def _extract_files(entry: dict) -> list[str]:
    """Extract proxyable file paths from tool_use blocks (Write, screenshot).

    Looks at assistant entries for Write tool calls whose file_path has an
    extension in _PROXY_FILE_EXTS.  Returns list of absolute file paths.
    """
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    files: list[str] = []
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        name = c.get("name") or ""
        inp = c.get("input")
        if not isinstance(inp, dict):
            continue
        # Write tool: file_path in input
        if name == "Write":
            fp = inp.get("file_path") or ""
            if isinstance(fp, str) and fp.startswith("/") and ".." not in fp:
                if os.path.splitext(fp)[1].lower() in _PROXY_FILE_EXTS:
                    files.append(fp)
        # browser_take_screenshot: output file path in input
        elif name == "browser_take_screenshot":
            fp = inp.get("path") or ""
            if isinstance(fp, str) and fp.startswith("/") and ".." not in fp:
                files.append(fp)
    return files


def _parse_catchup_delta(jsonl_path: Path, start: int, end: int,
                         is_initial: bool = False) -> dict:
    """Read JSONL byte range [start, end), pair user/assistant text turns.

    Returns {
      "entries": [...last N pairs as flat list of role/text dicts, up to
                  _CATCHUP_DETAIL_PAIRS pairs × 2 entries...],
      "earlier_count": int,   # number of turns outside the detail window
      "earlier_summary": str,  # preview line(s); empty when is_initial=True
    }

    When is_initial=True (first attach of this chat to JSONL), older turns
    are NOT preview-summarised — earlier_count alone is reported so the bot
    can render a compact "N earlier pairs before these" header. This keeps
    the first-attach digest bounded to last 10 pairs + a single count line
    regardless of how deep the pre-existing JSONL goes.
    """
    result: dict = {"entries": [], "earlier_count": 0, "earlier_summary": ""}
    if start >= end:
        return result

    # Helper: parse one line into a typed-turn dict (text|tool|skip).
    # Returns dict {"kind": "text"|"tool"|None, "role": str, "text": str,
    # "ts": str, "tools": list[str]} — or None to skip.
    def _parse_line(raw: bytes) -> "dict | None":
        if not raw.strip():
            return None
        if len(raw) > _CATCHUP_MAX_LINE_BYTES:
            # DoS guard: refuse pathological single-line payloads.
            log.warning("catchup skip oversized line bytes=%d", len(raw))
            return None
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(entry, dict):
            return None
        # D: skip API error turns — they're SDK/transport noise, not real
        # user/assistant content. Renders as fake assistant turns otherwise.
        # M3: log at debug so a cluster of skips is diagnosable in aggregate
        # (reviewer flagged silent drop as opacity risk).
        if entry.get("isApiErrorMessage") is True:
            log.debug("catchup skip isApiErrorMessage type=%s", entry.get("type"))
            return None
        etype = entry.get("type")
        ts = entry.get("timestamp") or ""
        if etype in ("user", "assistant"):
            txt = _extract_text(entry)
            tools = _extract_tool_names(entry) if etype == "assistant" else []
            files = _extract_files(entry) if etype == "assistant" else []
            return {
                "kind": "text" if txt else "tool_only",
                "role": etype,
                "text": txt,
                "ts": ts if isinstance(ts, str) else "",
                "tools": tools,
                "files": files,
            }
        return None

    all_turns: list[dict] = []
    tail_truncated = False

    if is_initial and (end - start) > _CATCHUP_REV_CHUNK:
        # Reverse-scan: seek blocks backwards from EOF until we have enough
        # pairs or run out of file / hit safety cap. Guarantees "exactly
        # last N pairs" regardless of tool-heavy density.
        target_pairs = _CATCHUP_DETAIL_PAIRS
        scanned = 0
        partial_head = b""
        rev_lines: list[bytes] = []  # lines in FILE order, prepended per pass
        pos = end
        try:
            f = jsonl_path.open("rb")
        except OSError as e:
            log.warning("catchup read failed path=%s: %s", jsonl_path, e)
            return result
        try:
            while pos > start and scanned < _CATCHUP_REV_SAFETY:
                read_start = max(start, pos - _CATCHUP_REV_CHUNK)
                read_len = pos - read_start
                f.seek(read_start)
                chunk = f.read(read_len)
                scanned += read_len
                pos = read_start
                buf = chunk + partial_head
                lines = buf.split(b"\n")
                # If we haven't reached file start, the very first element
                # may be a partial line continuation from an earlier line
                # above — stash as partial_head for the next pass.
                if pos > start and lines:
                    partial_head = lines[0]
                    lines = lines[1:]
                else:
                    partial_head = b""
                # Prepend in file order (lines already in file order within
                # buf after split; we just prepend to rev_lines).
                rev_lines = lines + rev_lines
                # Early exit if we have enough user-role text turns to
                # build N pairs. Count forward from tail.
                user_text_count = 0
                for raw in reversed(rev_lines):
                    parsed = _parse_line(raw)
                    if parsed and parsed["kind"] == "text" and parsed["role"] == "user":
                        user_text_count += 1
                        if user_text_count >= target_pairs + 1:
                            # N+1 so we have a bounded "earlier" anchor.
                            break
                if user_text_count >= target_pairs + 1:
                    break
            else:
                if scanned >= _CATCHUP_REV_SAFETY:
                    tail_truncated = True
                    result["earlier_count_approx"] = True
        finally:
            f.close()

        # Pass 1: walk rev_lines (in file order) building all_turns with
        # tool-chain carried between roles.
        pending_tools: list[str] = []
        for raw in rev_lines:
            parsed = _parse_line(raw)
            if not parsed:
                continue
            if parsed["kind"] == "tool_only":
                pending_tools.extend(parsed["tools"])
                continue
            turn = {"role": parsed["role"], "text": parsed["text"],
                    "ts": parsed["ts"], "tools": list(pending_tools)}
            if parsed["role"] == "assistant":
                # Assistant carries any tool_use calls that happened in its
                # own message too.
                turn["tools"] = list(pending_tools) + parsed["tools"]
            all_turns.append(turn)
            pending_tools = []
    else:
        # Delta mode (or small file) — read whole range forward.
        want = min(end - start, _CATCHUP_MAX_BYTES)
        try:
            with jsonl_path.open("rb") as f:
                f.seek(start)
                data = f.read(want)
        except OSError as e:
            log.warning("catchup read failed path=%s: %s", jsonl_path, e)
            return result
        pending_tools = []
        for raw in data.split(b"\n"):
            parsed = _parse_line(raw)
            if not parsed:
                continue
            if parsed["kind"] == "tool_only":
                pending_tools.extend(parsed["tools"])
                continue
            turn = {"role": parsed["role"], "text": parsed["text"],
                    "ts": parsed["ts"], "tools": list(pending_tools)}
            if parsed["role"] == "assistant":
                turn["tools"] = list(pending_tools) + parsed["tools"]
            all_turns.append(turn)
            pending_tools = []

    if tail_truncated:
        log.info("catchup reverse-scan hit safety cap bytes=%d", _CATCHUP_REV_SAFETY)

    if not all_turns:
        return result

    # Pass 2: group into query-response pairs (user then assistant).
    # Any stray leading assistant or dangling user at end is kept as a
    # single-sided pair so we don't drop it.
    pairs: list[list[dict]] = []
    cur: list[dict] = []
    for turn in all_turns:
        if turn["role"] == "user" and cur:
            # New user turn starts a new pair; flush previous
            pairs.append(cur)
            cur = [turn]
        else:
            cur.append(turn)
    if cur:
        pairs.append(cur)

    # Split into detail (last N pairs) + earlier (everything before)
    detail_pairs = pairs[-_CATCHUP_DETAIL_PAIRS:]
    earlier_pairs = pairs[:-_CATCHUP_DETAIL_PAIRS] if len(pairs) > _CATCHUP_DETAIL_PAIRS else []

    # Flatten detail, truncate each text to _CATCHUP_TRUNCATE_CHARS.
    # Each entry carries role, text, ts (ISO8601 string or ""), tools (list).
    for pair in detail_pairs:
        for t in pair:
            text = t["text"]
            if len(text) > _CATCHUP_TRUNCATE_CHARS:
                text = text[:_CATCHUP_TRUNCATE_CHARS] + "…"
            entry_dict: dict = {
                "role": t["role"],
                "text": text,
                "ts": t.get("ts", ""),
                "tools": t.get("tools", []),
            }
            if t.get("files"):
                entry_dict["files"] = t["files"]
            result["entries"].append(entry_dict)

    # Summarise earlier pairs: short previews of user queries.
    # On initial attach we report earlier_count only (no preview) so the digest
    # stays bounded regardless of JSONL depth.
    if earlier_pairs:
        earlier_count = sum(len(p) for p in earlier_pairs)
        result["earlier_count"] = earlier_count
        if not is_initial:
            previews: list[str] = []
            for pair in earlier_pairs:
                user_turn = next((t for t in pair if t["role"] == "user"), None)
                if user_turn:
                    t = user_turn["text"]
                    if len(t) > _CATCHUP_SUMMARY_PREVIEW_CHARS:
                        t = t[:_CATCHUP_SUMMARY_PREVIEW_CHARS] + "…"
                    previews.append(t.replace("\n", " ").strip())
            # Keep first + last preview if many (lean summary for large backlogs)
            if len(previews) > 4:
                previews = previews[:2] + ["…"] + previews[-2:]
            result["earlier_summary"] = "\n• ".join(previews)

    return result


# ─────────────────────────────────────────────────────────────────────────
# PID + state file management (orphan-safe)
# ─────────────────────────────────────────────────────────────────────────

def _sanitise_chat_id(raw: str) -> str:
    """Accept only `^-?[0-9]+$`. Fail fast to prevent filename traversal."""
    if not _CHAT_ID_RE.match(raw):
        raise SystemExit(f"invalid_chat_id:{raw[:40]!r}")
    return raw


def _pid_alive_and_ours(pid: int) -> bool:
    """True iff pid is alive AND its cmdline references this script name."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # cmdline check — macOS: use `ps -p <pid> -o command=`
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return "sdk_persistent_daemon" in (out.stdout or "")
    except Exception:
        return True  # err on the side of "could be ours"; ping will sort it


def _check_or_claim_pidfile(chat_id: str) -> None:
    """Orphan-safe pidfile claim. Exits with daemon_error if another live daemon exists."""
    _DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_path = _DAEMONS_DIR / f"{chat_id}.pid"
    state_path = _DAEMONS_DIR / f"{chat_id}.state"

    if pid_path.exists():
        try:
            existing = int(pid_path.read_text().strip())
        except Exception:
            existing = 0
        if existing and _pid_alive_and_ours(existing):
            _emit({"type": "error", "error": f"already_running:pid={existing}"})
            log.warning("Another daemon already live: pid=%d for chat=%s", existing, chat_id)
            raise SystemExit(3)
        # Orphan — clean up
        log.info("Reaping orphan pidfile: pid=%s chat=%s", existing, chat_id)
        for p in (pid_path, state_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    my_pid = os.getpid()
    tmp = pid_path.with_suffix(".pid.tmp")
    tmp.write_text(str(my_pid))
    os.replace(tmp, pid_path)


def _write_state(chat_id: str, **fields) -> None:
    """Atomic state snapshot write."""
    state_path = _DAEMONS_DIR / f"{chat_id}.state"
    tmp = state_path.with_suffix(".state.tmp")
    try:
        tmp.write_text(json.dumps(fields, default=str))
        os.replace(tmp, state_path)
    except Exception as e:
        log.warning("state write failed: %s", e)


def _cleanup_pidfile(chat_id: str) -> None:
    pid_path = _DAEMONS_DIR / f"{chat_id}.pid"
    state_path = _DAEMONS_DIR / f"{chat_id}.state"
    for p in (pid_path, state_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("cleanup %s failed: %s", p, e)


# ─────────────────────────────────────────────────────────────────────────
# Claude session wrapper
# ─────────────────────────────────────────────────────────────────────────

class _DaemonSession:
    """Wraps claude-agent-sdk ClaudeSDKClient, pinned to a single JSONL UUID.

    Lifecycle: one `claude --print --input-format stream-json --resume UUID`
    subprocess is spawned on first query and kept alive with stdin OPEN across
    subsequent queries, so a new query arriving mid-flight of a prior one can
    be injected onto the same stdin pipe (primary-chat parity — see
    `relay_cli_stdin_reentrant` fact, Apr 17 2026 verification).

    The reader task continuously drains claude stdout, tagging each stream
    entry with the head of a FIFO `_pending_qids` deque. Each `result` entry
    pops the head and signals that qid's completion event. When the deque
    empties, a grace-period drain task closes stdin so the subprocess exits
    cleanly; any new query arriving during grace cancels the drain.
    """

    def __init__(self, chat_id: str, uuid: str, cwd: str):
        self.chat_id = chat_id
        self.uuid = uuid
        self.cwd = cwd
        self.started_at = time.time()
        self.last_activity = time.time()
        self.queries_served = 0
        self.current_query_id: str | None = None
        self.detected_model: str | None = None
        self.detected_settings: dict[str, str | None] = {}
        # Persistent claude subprocess + reader state.
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_stdin_lock = asyncio.Lock()
        self._pending_qids: collections.deque[str] = collections.deque()
        self._qid_events: dict[str, asyncio.Event] = {}
        self._qid_results_seen: set[str] = set()
        self._reader_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        # Merge-detection window: count of user-role stream entries with
        # non-empty TEXT content seen since last `result` event. Used to
        # precisely detect how many injected user frames the CLI merged
        # into a single result (num_turns alone is ambiguous — see below).
        # Reset to 0 at the START of each `result` handler before counting
        # the current entry against the NEXT window.
        self._user_text_in_window: int = 0
        # Realtime-tailer dedup state (item 2).
        # deque + set pair: O(1) membership + bounded FIFO eviction via
        # explicit popleft (NOT deque(maxlen=) — which silently drops without
        # giving us the evicted value, breaking set sync).
        self._own_message_ids: collections.deque[str] = collections.deque()
        self._own_message_id_set: set[str] = set()
        # Own-write offset ranges. Each entry is a 2-element list [start, end]
        # where end=None means "query still running" (treat as ∞). List (not
        # tuple) so we can mutate end in-place in the reader-loop drain.
        self._own_write_ranges: collections.deque[list[int | None]] = \
            collections.deque(maxlen=_REALTIME_OWN_RANGES_CAP)
        self._tail_seq: int = 0
        self._tail_last_ino: int | None = None
        self._tail_pending: bytes = b""  # partial-line defer across ticks
        self._realtime_turns_forwarded: int = 0
        self._persist_state("starting")
        # One-shot attach catchup: emit last N turns from JSONL to TG ONCE per
        # daemon lifetime (decoupled from query lifecycle). Bot reads the event
        # from stdout and posts a separate TG message before any query reply
        # streams. NOT injected into model context — JSONL is already the
        # model's history via `claude --resume UUID`.
        # Internal _load_offset guard makes this idempotent (no-op after first
        # call for this chat+uuid). Bound = _CATCHUP_DETAIL_PAIRS (10) pairs.
        if self.uuid != _NEW_SESSION:
            try:
                self._emit_desktop_catchup("attach")
            except Exception as _e:
                log.warning("attach catchup emission failed chat=%s: %s",
                            self.chat_id, _e)

    def _record_own_message_id(self, mid: str | None) -> None:
        """Register a message.id as daemon-originated. Bounded FIFO (cap=
        _REALTIME_DEDUP_CAP). Safe to call with None / empty string (no-op).
        """
        if not mid or not isinstance(mid, str):
            return
        if mid in self._own_message_id_set:
            return
        while len(self._own_message_ids) >= _REALTIME_DEDUP_CAP:
            old = self._own_message_ids.popleft()
            self._own_message_id_set.discard(old)
        self._own_message_ids.append(mid)
        self._own_message_id_set.add(mid)

    def _is_own_write_offset(self, offset: int) -> bool:
        """Return True if `offset` falls inside any recorded daemon-write
        byte range. `end is None` means the range is still open (query live).
        Belt-and-suspenders for entries lacking message.id.
        """
        for rng in self._own_write_ranges:
            start = rng[0]
            end = rng[1]
            if end is None:
                if offset >= start:
                    return True
            else:
                if start <= offset < end:
                    return True
        return False

    def _persist_state(self, status: str) -> None:
        _write_state(
            self.chat_id,
            status=status,
            uuid=self.uuid,
            cwd=self.cwd,
            started_at=self.started_at,
            last_heartbeat=time.time(),
            queries_served=self.queries_served,
            current_query_id=self.current_query_id,
            pid=os.getpid(),
            model=self.detected_model,
            effort=self.detected_settings.get("effort"),
        )

    def mark_idle(self) -> None:
        self.current_query_id = None
        self.last_activity = time.time()
        self._persist_state("idle")

    def mark_busy(self, query_id: str) -> None:
        self.current_query_id = query_id
        self.last_activity = time.time()
        self._persist_state("busy")

    async def run_query(self, query_id: str, text: str) -> None:
        """Push a user frame onto the persistent claude subprocess's stdin.

        Mid-task injection path: if a claude subprocess is already running for
        a prior query, this writes the new frame onto the SAME stdin pipe —
        claude processes it as a new turn at its next input checkpoint (AP-1
        verified Apr 17 2026, see fact relay_cli_stdin_reentrant).

        Cold path: if no subprocess is alive, spawn a fresh one and write the
        frame. Either way, the reader-loop task handles stream/result emission
        keyed by FIFO qid. This coroutine blocks until THIS qid's result
        entry has been emitted (or an error/EOF broadcast closes its event).
        """
        self.mark_busy(query_id)
        ev = asyncio.Event()
        self._qid_events[query_id] = ev
        try:
            # NOTE: catchup emission moved to __init__ (one-shot per daemon
            # lifetime, decoupled from query lifecycle). See attach_emit_catchup
            # below. Per-query catchup retired Apr 18 2026 — was firing on first
            # query for fresh chat+uuid bindings, which felt like incoming-query
            # ingestion to the user.
            await self._ensure_proc_and_write(query_id, text)
            # Wait for the reader loop to signal this qid is done. Cap the
            # wait at 3h (heavy coding queries can run 47+ tools); if
            # exceeded, claude is likely stuck — surface an error and let
            # the caller decide.
            try:
                await asyncio.wait_for(ev.wait(), timeout=3 * 3600)
            except asyncio.TimeoutError:
                _emit({"type": "error", "query_id": query_id,
                       "error": "query_timeout_3h"})
        finally:
            self._qid_events.pop(query_id, None)
            self._qid_results_seen.discard(query_id)
            self._commit_offset_to_eof()
            self.queries_served += 1
            if not self._pending_qids:
                self.mark_idle()

    def _emit_desktop_catchup(self, query_id: str) -> None:
        """Emit attach digest: last _CATCHUP_DETAIL_PAIRS query-response pairs
        from the resumed JSONL, sent ONCE per daemon lifetime from __init__.
        Decoupled from query lifecycle so incoming TG queries never trigger
        backlog ingestion.

        Two modes (Apr 18 2026 update):
        - first_run (offset is None): chat is freshly attached to this UUID
          (new relay chat, or re-pinned to a different UUID). Parse full file,
          surface last 10 pairs, baseline offset to EOF.
        - delta (offset exists): daemon respawned (post-offline / manual kill).
          Parse offset..EOF, surface last 10 pairs of NEW entries since saved
          offset. Skip emission if delta is empty.

        Output: `catchup` stream-json event with `cause` field ("first_run"
        or "delta"). Bot reads it and renders an appropriate TG header.
        NOT injected into model context — JSONL is the model's native history
        via `claude --resume UUID`.

        `query_id` is informational only (always "attach" from __init__).
        """
        jsonl = _find_jsonl(self.uuid)
        if not jsonl:
            # QA case 2 fix: pinning to a non-existent UUID (typo, deleted
            # JSONL, etc.) used to exit silently — user got zero feedback.
            # Surface a warning event so the bot can notify the user.
            log.warning("catchup: no JSONL found for chat=%s uuid=%s",
                        self.chat_id, self.uuid)
            _emit({
                "type": "catchup_warning",
                "query_id": query_id,
                "subtype": "missing_jsonl",
                "uuid": self.uuid,
            })
            return
        try:
            size = jsonl.stat().st_size
        except OSError:
            return

        stored = _load_offset(self.chat_id, self.uuid)
        is_first_run = stored is None
        cause = "first_run" if is_first_run else "delta"
        # Delta mode: start from MAX(saved offset, saved tail offset).
        # tail_offset is the realtime tailer's high-water mark — it already
        # advanced past any bytes the previous daemon processed (including
        # own-write-deduped trailing writes from subprocess). Using the max
        # ensures we don't re-surface entries the tailer already handled.
        # Fixes Issue 3 (phantom "1 new turn" delta catchup after respawn
        # caused by trailing subprocess writes that the previous daemon
        # had already covered via its open own-write range).
        start = 0 if is_first_run else min(
            max(int(stored or 0),
                _load_tail_offset(self.chat_id, self.uuid)),
            size,
        )

        if size == 0:
            # Empty JSONL — baseline offsets to 0, no event.
            _save_offset(self.chat_id, self.uuid, 0)
            _save_tail_offset(self.chat_id, self.uuid, 0)
            return

        # Parse the relevant byte range. is_initial=True only for first_run
        # (gates earlier_summary suppression). Delta mode includes summary.
        parsed = _parse_catchup_delta(jsonl, start, size, is_initial=is_first_run)
        entries = parsed["entries"]

        if not entries and not parsed["earlier_count"]:
            # Delta with no new content — keep stored offset at its existing
            # value (already at prior EOF). Don't advance: that would mask a
            # later first_run check on this chat+uuid if the file grows.
            # For first_run mode reaching here (file existed but had no
            # parseable user/assistant text), we still want to baseline so
            # the next spawn doesn't redo this work.
            if is_first_run:
                _save_offset(self.chat_id, self.uuid, size)
                _save_tail_offset(self.chat_id, self.uuid, size)
            return

        # Emit FIRST, then persist offsets. Architect AP-22: persisting
        # before emit risks silent loss if the daemon is killed between
        # save and emit (offset advances → next respawn sees empty delta →
        # backlog never delivered). Emitting first means at-least-once
        # delivery: if the daemon dies before save, the next spawn re-emits
        # (idempotent because the bot deduplicates by realtime tailer's
        # message.id set). If save fails after emit, next spawn re-emits
        # — acceptable double-delivery rather than silent loss.
        _emit({
            "type": "catchup",
            "query_id": query_id,
            "cause": cause,
            "entries": entries,
            "earlier_count": parsed["earlier_count"],
            "earlier_summary": parsed["earlier_summary"],
            "is_initial": is_first_run,
            # QA case 14: set when first_run hit the 2 MB tail window — count
            # reflects only the parsed tail, not the full JSONL history.
            "tail_truncated": bool(parsed.get("earlier_count_approx")),
        })
        _save_offset(self.chat_id, self.uuid, size)
        _save_tail_offset(self.chat_id, self.uuid, size)
        log.info("catchup emitted chat=%s qid=%s cause=%s entries=%d earlier=%d",
                 self.chat_id, query_id, cause,
                 len(entries), parsed["earlier_count"])

    def _commit_offset_to_eof(self) -> None:
        """Advance BOTH synced offsets past this query's JSONL appends.

        Called AFTER run_query completes so the daemon's own writes become
        the new baseline — next query only surfaces Desktop writes that land
        during the idle window.

        Saves `_save_offset` (catchup-delta baseline) AND `_save_tail_offset`
        (realtime tailer baseline). Keeping them in sync prevents Issue 3:
        a stale tail_offset before a daemon respawn would cause the delta
        catchup to show phantom "1 new turn" entries that are actually
        trailing writes from this daemon's own subprocess.
        """
        jsonl = _find_jsonl(self.uuid)
        if not jsonl:
            return
        try:
            size = jsonl.stat().st_size
            _save_offset(self.chat_id, self.uuid, size)
            _save_tail_offset(self.chat_id, self.uuid, size)
        except OSError:
            pass

    async def _ensure_proc_and_write(self, query_id: str, text: str) -> None:
        """Ensure a claude subprocess is alive, then write a user frame.

        Under `_proc_stdin_lock` (short-held, milliseconds). If the subprocess
        is dead/missing, spawn a fresh one and start the reader loop. If an
        impending drain task was scheduled (deque went empty), cancel it —
        we have a new qid to serve. Always push qid onto FIFO and record a
        fresh own-write range for the tailer's dedup.

        The bulk of work (reading stdout, emitting stream/result entries) is
        done by `_reader_loop`, which persists across queries on the same
        subprocess. This method returns quickly once the frame is written.
        """
        import shutil
        claude_bin = shutil.which("claude")
        if not claude_bin:
            _emit({"type": "error", "query_id": query_id,
                   "error": "claude_binary_not_found"})
            ev = self._qid_events.get(query_id)
            if ev:
                ev.set()
            return

        user_msg = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        stdin_payload = (json.dumps(user_msg) + "\n").encode("utf-8")

        async with self._proc_stdin_lock:
            # Cancel any pending drain task — we have new work.
            if self._drain_task is not None and not self._drain_task.done():
                self._drain_task.cancel()
                self._drain_task = None

            # Guard: if stdin is already closing (drain task ran or process is
            # winding down), force-kill the lingering process so we can respawn
            # immediately.  The old approach (return error, hope proc exits soon)
            # caused a zombie-alive state: stdin closed but proc alive with
            # returncode=None — the reader loop never saw EOF because the
            # subprocess was slow to exit, blocking ALL subsequent queries
            # with repeated proc_stdin_closing errors (FB-53).
            proc = self._proc
            if (proc is not None
                    and proc.returncode is None
                    and (proc.stdin is None or proc.stdin.is_closing())):
                log.warning("inject qid=%s: stdin closing — force-killing "
                            "lingering proc (pid=%s) for respawn",
                            query_id, proc.pid)
                try:
                    proc.kill()
                except Exception:
                    pass
                # Give it a moment to die so returncode is set.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                # Clear proc ref so the spawn path below fires.
                if self._proc is proc:
                    self._proc = None
                proc = None
                # Fall through to the spawn path — do NOT return an error.

            # Spawn if dead/missing.
            if proc is None or proc.returncode is not None:
                cmd = [
                    claude_bin, "--print",
                    "--output-format", "stream-json",
                    "--input-format", "stream-json",
                    "--verbose",
                    "--permission-mode", "acceptEdits",
                    "--dangerously-skip-permissions",
                    "--include-partial-messages",
                    "--replay-user-messages",
                ]
                # Resume existing session
                if self.uuid != _NEW_SESSION:
                    cmd.extend(["--resume", self.uuid])

                # Model selection priority:
                # 1. RELAY_MODEL_OVERRIDE env var (bot-side per-relay override)
                # 2. Auto-detect from JSONL (match Desktop.app's model)
                # 3. None (CLI default)
                model_override = os.environ.get("RELAY_MODEL_OVERRIDE", "")
                if model_override:
                    cmd.extend(["--model", model_override])
                    self.detected_model = model_override
                    detected_model = model_override
                elif self.uuid != _NEW_SESSION:
                    detected_model = _detect_model_from_jsonl(self.uuid)
                    if detected_model:
                        cmd.extend(["--model", detected_model])
                        self.detected_model = detected_model
                else:
                    detected_model = None

                # Effort override from bot-side per-relay setting
                effort_override = os.environ.get("RELAY_EFFORT_OVERRIDE", "")
                if effort_override:
                    cmd.extend(["--effort", effort_override])
                    self.detected_settings["effort"] = effort_override
                log.info("spawn claude qid=%s cwd=%s model=%s effort=%s",
                         query_id, self.cwd,
                         detected_model or "(default)",
                         effort_override or self.detected_settings.get("effort") or "(default)")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, cwd=self.cwd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=10 * 1024 * 1024,  # match bot-side 10MB cap
                    )
                except Exception as e:
                    _emit({"type": "error", "query_id": query_id,
                           "error": f"spawn_failed:{e}"})
                    ev = self._qid_events.get(query_id)
                    if ev:
                        ev.set()
                    return
                self._proc = proc
                # Start reader loop for this subprocess. Reader persists for
                # the subprocess's lifetime and broadcasts errors on EOF.
                self._reader_task = asyncio.create_task(
                    self._reader_loop(proc))

            # Record own-write range for the tailer's dedup.
            jsonl_at_start = _find_jsonl(self.uuid)
            start_size = 0
            if jsonl_at_start is not None:
                try:
                    start_size = jsonl_at_start.stat().st_size
                except OSError:
                    start_size = 0
            self._own_write_ranges.append([start_size, None])

            # Push qid onto FIFO, then write the user frame.
            self._pending_qids.append(query_id)
            log.info("inject qid=%s pending=%d bytes=%d",
                     query_id, len(self._pending_qids), len(text))
            try:
                assert proc.stdin is not None
                proc.stdin.write(stdin_payload)
                await proc.stdin.drain()
            except Exception as e:
                _emit({"type": "error", "query_id": query_id,
                       "error": f"stdin_write_failed:{e}"})
                # Pop the qid we just pushed (FIFO invariant).
                try:
                    self._pending_qids.remove(query_id)
                except ValueError:
                    pass
                # Close the open own-write range — no claude output will
                # accompany this frame.
                if self._own_write_ranges and self._own_write_ranges[-1][1] is None:
                    self._own_write_ranges[-1][1] = start_size
                ev = self._qid_events.get(query_id)
                if ev:
                    ev.set()

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        """Drain claude stdout for the lifetime of `proc`.

        Tags every entry with the CURRENT head of `_pending_qids`. When a
        `result` entry arrives, pops the head qid, finalises its open
        own-write range, and signals the qid's completion event. On EOF or
        unrecoverable error, broadcasts an error for any qids still pending
        and signals their events so callers don't block forever.
        """
        assert proc.stdout is not None
        line_count = 0
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError as ve:
                    # 10MB buffer overflow — tell bot, bail
                    head_qid = self._pending_qids[0] if self._pending_qids else ""
                    _emit({"type": "error", "query_id": head_qid,
                           "error": f"stream_line_overflow:{str(ve)[:120]}"})
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                if not line:
                    break
                s = line.decode("utf-8", errors="replace").rstrip("\n")
                if not s:
                    continue
                # Any stdout activity means the subprocess is still doing
                # work — cancel any pending drain so we don't close stdin
                # while tool operations (git auth, git push, etc.) are
                # still in flight.
                if self._drain_task is not None and not self._drain_task.done():
                    self._drain_task.cancel()
                    self._drain_task = None
                try:
                    entry = json.loads(s)
                except json.JSONDecodeError:
                    head_qid = self._pending_qids[0] if self._pending_qids else ""
                    _emit({"type": "stream", "query_id": head_qid,
                           "entry": {"type": "opaque", "raw": s[:4000]}})
                    continue
                etype = entry.get("type") if isinstance(entry, dict) else None
                # Register own message.ids for realtime-tailer dedup.
                if etype in ("user", "assistant"):
                    try:
                        mid = (entry.get("message") or {}).get("id")
                        self._record_own_message_id(mid)
                    except Exception:
                        pass
                    # Count user-role entries with non-empty TEXT content —
                    # this is precisely the signal the CLI emits for each
                    # user-initiated conversational turn (injected or not).
                    # Tool_result entries (which also have type='user' in
                    # CLI output) are excluded because _extract_text returns
                    # '' for content lacking a `type=text` block. Used for
                    # accurate merge detection on the next `result` event.
                    if etype == "user":
                        try:
                            if _extract_text(entry):
                                self._user_text_in_window += 1
                        except Exception as _ute:
                            # Defensive: _extract_text is a pure dict walker
                            # today and shouldn't throw, but if a future
                            # refactor adds regex / decode steps, a silent
                            # undercount here → missed drain → hung placeholder.
                            # Log a WARNING so the failure mode is observable.
                            log.warning(
                                "user-text count skipped (extract failed): %s",
                                _ute,
                            )
                head_qid = self._pending_qids[0] if self._pending_qids else ""
                if etype == "result":
                    # Finalise this turn: emit result tagged with head qid,
                    # pop it, finalise its own-write range, signal its event.
                    _emit({"type": "result", "query_id": head_qid,
                           "subtype": entry.get("subtype", ""),
                           "cost_usd": entry.get("cost_usd"),
                           "turns": entry.get("num_turns"),
                           "raw": entry})
                    if head_qid:
                        try:
                            self._pending_qids.popleft()
                        except IndexError:
                            pass
                        self._qid_results_seen.add(head_qid)
                        # NOTE (Apr 18 2026): we do NOT close the own-write
                        # range here. claude --print continues writing JSONL
                        # for a brief window AFTER emitting the `result`
                        # stream-json event (trailing metadata, summary
                        # audit entries). If we closed the range at this
                        # point, those trailing bytes would fall outside
                        # any own-range, causing the realtime tailer to
                        # emit them as spurious "Desktop activity" turns
                        # (Issue 4) and the next daemon respawn to show
                        # them as phantom delta catchup (Issue 3). Instead,
                        # ranges stay open until the subprocess actually
                        # exits — the post-EOF cleanup block below closes
                        # all remaining open ranges at true EOF.
                        ev = self._qid_events.get(head_qid)
                        if ev:
                            ev.set()
                        log.info("result qid=%s remaining_pending=%d",
                                 head_qid, len(self._pending_qids))
                    # MERGE DETECTION (precise, window-based): the CLI
                    # absorbs injected frames into the current AgentLoop
                    # when the parent turn is multi-turn. One `result`
                    # event can cover multiple user frames — each absorbed
                    # frame manifests as a distinct user-role stream-json
                    # entry with non-empty TEXT content (counted above in
                    # _user_text_in_window). We drain EXACTLY
                    # `extras = user_text_count - 1` additional qids here
                    # (the first user-text entry maps to head_qid, which
                    # we already popped). Not using `num_turns` because
                    # that counts agent-loop iterations — a single user
                    # frame with multiple tool rounds also reports
                    # num_turns>1, which would trigger a FALSE drain.
                    # Bounded by len(_pending_qids) so we never over-drain
                    # if the user's queued more qids than CLI absorbed.
                    # Window counter reset to 0 before leaving this branch
                    # so the next result starts a fresh count.
                    # Empirical basis: fact `relay_injection_semantics_verified`.
                    user_text_count = self._user_text_in_window
                    self._user_text_in_window = 0
                    if user_text_count > 1 and self._pending_qids:
                        extras = min(user_text_count - 1,
                                     len(self._pending_qids))
                        for _ in range(extras):
                            if not self._pending_qids:
                                break
                            merged_qid = self._pending_qids.popleft()
                            _emit({"type": "result",
                                   "query_id": merged_qid,
                                   "subtype": "merged_into_prior_qid",
                                   "cost_usd": 0.0,
                                   "turns": 0,
                                   "raw": {"merged_into": head_qid}})
                            self._qid_results_seen.add(merged_qid)
                            # NOTE (Apr 18 2026): same rationale as the
                            # head-qid branch above — do NOT close the
                            # own-write range here. Let post-EOF cleanup
                            # handle it at subprocess exit.
                            mev = self._qid_events.get(merged_qid)
                            if mev:
                                mev.set()
                            log.info("merged-drain qid=%s into=%s "
                                     "(user_text_in_window=%d extras=%d)",
                                     merged_qid, head_qid,
                                     user_text_count, extras)
                    # NEW SESSION DISCOVERY: if this was a fresh session
                    # (uuid == NEW), discover the JSONL that Claude just
                    # created and emit a new_session event so the bot can
                    # update the registry. Also update self.uuid so
                    # subsequent queries use --resume.
                    if self.uuid == _NEW_SESSION:
                        try:
                            found = _find_newest_jsonl_for_cwd(self.cwd)
                            if found:
                                new_uuid, new_path = found
                                self.uuid = new_uuid
                                _emit({"type": "new_session",
                                       "chat_id": self.chat_id,
                                       "uuid": new_uuid,
                                       "jsonl_path": new_path})
                                log.info("new session discovered: uuid=%s path=%s",
                                         new_uuid, new_path)
                        except Exception as _nse:
                            log.warning("new session discovery failed: %s", _nse)
                    # If FIFO is empty, schedule a graceful drain — closes
                    # stdin after a small grace window. A fresh inject
                    # during the grace window cancels the drain.
                    if not self._pending_qids:
                        if self._drain_task is None or self._drain_task.done():
                            self._drain_task = asyncio.create_task(
                                self._drain_and_close(proc))
                else:
                    _emit({"type": "stream", "query_id": head_qid, "entry": entry})
                line_count += 1
        except Exception as e:
            log.warning("reader loop error: %s", e, exc_info=True)

        # Subprocess exited or EOF — broadcast error to any leftover qids and
        # wake their waiters so run_query() returns instead of hanging.
        try:
            rc = proc.returncode
            if rc is None:
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    rc = -9
        except Exception:
            rc = -1
        stderr_tail = ""
        try:
            if proc.stderr is not None:
                stderr_tail = (await proc.stderr.read()).decode(
                    "utf-8", errors="replace")[:400]
        except Exception:
            pass
        log.info("reader loop exit rc=%s lines=%d pending=%d stderr=%s",
                 rc, line_count, len(self._pending_qids), stderr_tail[:120])
        # Broadcast error for any qid still pending (subprocess died before
        # its result was emitted).
        while self._pending_qids:
            qid = self._pending_qids.popleft()
            if qid in self._qid_results_seen:
                continue  # already completed — defensive
            _emit({"type": "error", "query_id": qid,
                   "error": f"subprocess_eof_rc{rc}:{stderr_tail.strip()}"[:400]})
            ev = self._qid_events.get(qid)
            if ev:
                ev.set()
        # Close any remaining open own-write ranges.
        for rng in self._own_write_ranges:
            if rng[1] is None:
                try:
                    jsonl_now = _find_jsonl(self.uuid)
                    rng[1] = (jsonl_now.stat().st_size
                              if jsonl_now else rng[0] or 0)
                except OSError:
                    rng[1] = rng[0] or 0
        # Clear subprocess ref so next query spawns fresh.
        if self._proc is proc:
            self._proc = None

    async def _drain_and_close(self, proc: asyncio.subprocess.Process) -> None:
        """Grace-period drain: if subprocess stays idle, close stdin.

        Waits _SUBPROCESS_IDLE_TIMEOUT_S (default 300s) before closing.
        A fresh inject during this window CANCELS the task (see cancellation
        handling in `_ensure_proc_and_write`).  Any stdout activity from the
        subprocess also cancels via the reader loop (covers long-running
        tool operations like git auth/push that continue after result).
        Cancellation leaves the subprocess alive with stdin open for the
        new frame.  Natural completion closes stdin, letting claude exit
        cleanly; the reader loop will observe EOF and wind down.

        After closing stdin, waits _SUBPROCESS_STDIN_CLOSE_KILL_S for the
        subprocess to exit.  If it lingers (zombie-alive state), force-kills
        it and clears self._proc so the next query can respawn (FB-53).
        """
        try:
            await asyncio.sleep(_SUBPROCESS_IDLE_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        async with self._proc_stdin_lock:
            if self._pending_qids:
                # New inject landed — reader will schedule another drain later.
                return
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            log.info("drain: stdin closed pending=0 queries_served=%d",
                     self.queries_served)
        # Post-close kill watchdog: if the subprocess doesn't exit within
        # _SUBPROCESS_STDIN_CLOSE_KILL_S, force-kill it to prevent the
        # zombie-alive state where stdin is closed but proc lingers with
        # returncode=None, blocking all subsequent queries (FB-53).
        try:
            await asyncio.wait_for(proc.wait(),
                                   timeout=_SUBPROCESS_STDIN_CLOSE_KILL_S)
        except asyncio.TimeoutError:
            log.warning("drain: proc pid=%s lingering %ds after stdin close "
                        "— force-killing (FB-53)",
                        proc.pid, _SUBPROCESS_STDIN_CLOSE_KILL_S)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            # Clear proc ref so next query spawns fresh.
            if self._proc is proc:
                self._proc = None
        except asyncio.CancelledError:
            # Drain was cancelled (new inject arrived) after stdin already closed.
            # _ensure_proc_and_write will detect stdin.is_closing() and force-kill.
            return
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # Realtime tailer (item 2 — always-on Desktop→TG catchup)
    # ─────────────────────────────────────────────────────────────────────

    def _read_slice_blocking(self, path: Path, start: int, end: int) -> bytes:
        """Blocking read of `path[start:end]`. Capped at _REALTIME_SLICE_MAX_BYTES.
        Returns empty bytes on OSError. Intended for asyncio.to_thread dispatch.
        """
        want = min(max(0, end - start), _REALTIME_SLICE_MAX_BYTES)
        if want <= 0:
            return b""
        try:
            with path.open("rb") as f:
                f.seek(start)
                return f.read(want)
        except OSError:
            return b""

    def _iter_complete_lines(self, data: bytes, base_offset: int) -> \
            tuple[list[tuple[int, bytes]], bytes]:
        """Split `data` into complete lines (ending in \\n) + trailing partial.

        Returns (lines, trailing) where:
        - lines: list of (absolute_offset, line_bytes_without_newline)
        - trailing: bytes after the last \\n (partial line; prepend next tick)

        Handles UTF-8 boundaries implicitly by deferring anything after the
        last newline to the next read (since JSONL lines are \\n-terminated,
        a partial line necessarily ends mid-content not mid-char — the
        remaining bytes are either the rest of a valid UTF-8 line or a
        truncated multi-byte sequence that will complete on the next tick).
        """
        lines: list[tuple[int, bytes]] = []
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return lines, data
        complete = data[: last_nl + 1]
        trailing = data[last_nl + 1 :]
        offset = base_offset
        for piece in complete.split(b"\n"):
            line_len = len(piece) + 1  # +1 for the \n that was stripped by split
            if piece:
                lines.append((offset, piece))
            offset += line_len
        # Last element of split() on "...\n" is empty bytes; offset bookkeeping
        # above already accounts for its trailing newline.
        return lines, trailing

    async def _realtime_tail_loop(self) -> None:
        """Poll the pinned JSONL and forward Desktop-typed turns in realtime.

        Dedup order (correctness-critical):
        1. Parse line → extract message.id
        2. If message.id in self._own_message_id_set → skip (definitely own)
        3. If message.id present and NOT own → emit (definitely Desktop —
           even if offset falls in an open own-write range)
        4. If no message.id and offset in own-write range → skip
        5. Else → emit

        Never pauses during active queries — dedup set + ranges handle the
        concurrent-write case. Broad try/except: tailer failure never disables
        query servicing (graceful degradation, principle 10).
        """
        await asyncio.sleep(_REALTIME_STARTUP_DELAY_S)
        # Baseline offset: seed from saved tail-offset, else current EOF so
        # we don't replay pre-existing backlog (_emit_desktop_catchup handles
        # first-attach digest).
        current_offset = _load_tail_offset(self.chat_id, self.uuid)
        if current_offset == 0:
            jsonl = _find_jsonl(self.uuid)
            if jsonl is not None:
                try:
                    current_offset = jsonl.stat().st_size
                    _save_tail_offset(self.chat_id, self.uuid, current_offset)
                except OSError:
                    current_offset = 0
        log.info("realtime tail start chat=%s uuid=%s base_offset=%d",
                 self.chat_id, self.uuid, current_offset)

        while True:
            try:
                jsonl = _find_jsonl(self.uuid)
                if jsonl is None:
                    await asyncio.sleep(2.0)
                    continue
                try:
                    st = jsonl.stat()
                except FileNotFoundError:
                    await asyncio.sleep(2.0)
                    continue
                # Rotation: inode change means a new JSONL file (Desktop
                # reopened project with fresh uuid, or rename). Reset baseline.
                if self._tail_last_ino is not None and st.st_ino != self._tail_last_ino:
                    log.warning("realtime tail rotation chat=%s old_ino=%s new_ino=%s",
                                self.chat_id, self._tail_last_ino, st.st_ino)
                    _emit({"type": "desktop_turn_meta", "subtype": "jsonl_rotated",
                           "chat_id": self.chat_id, "uuid": self.uuid})
                    current_offset = 0
                    self._tail_pending = b""
                self._tail_last_ino = st.st_ino
                # Truncation: file shrank. Reset silently.
                if st.st_size < current_offset:
                    current_offset = 0
                    self._tail_pending = b""
                if st.st_size > current_offset:
                    t0 = time.monotonic()
                    slice_start = current_offset
                    slice_end = st.st_size
                    data = await asyncio.to_thread(
                        self._read_slice_blocking, jsonl, slice_start, slice_end)
                    if not data:
                        await asyncio.sleep(_REALTIME_TAIL_INTERVAL_S)
                        continue
                    # F-04: cap pending-line buffer to prevent unbounded
                    # growth on newline-free / corrupted input. If we've
                    # accumulated > _REALTIME_LINE_MAX_BYTES without seeing
                    # a '\n', discard the buffer — it's either corrupt or
                    # a pathological single line we'd skip anyway.
                    if len(self._tail_pending) > _REALTIME_LINE_MAX_BYTES:
                        log.warning("realtime tail pending overflow chat=%s "
                                    "discarded=%d",
                                    self.chat_id, len(self._tail_pending))
                        self._tail_pending = b""
                    # Prepend any partial-line bytes held over from last tick.
                    combined = self._tail_pending + data
                    # Absolute offset of combined[0] in the file:
                    combined_base = slice_start - len(self._tail_pending)
                    lines, trailing = self._iter_complete_lines(combined, combined_base)
                    self._tail_pending = trailing
                    # Advance offset to cover consumed bytes (complete lines only).
                    consumed = len(combined) - len(trailing)
                    new_offset = combined_base + consumed
                    # Detect slice cap saturation → warn + skip remainder.
                    # slice_cap_fired is used below to prevent the
                    # suppressed-offset cap from overriding this safety valve.
                    slice_cap_fired = False
                    if (slice_end - slice_start) >= _REALTIME_SLICE_MAX_BYTES:
                        log.warning("realtime tail slice_cap chat=%s bytes=%d",
                                    self.chat_id, slice_end - slice_start)
                        _emit({"type": "desktop_turn_meta", "subtype": "slice_skipped",
                               "chat_id": self.chat_id, "uuid": self.uuid,
                               "bytes": slice_end - slice_start})
                        new_offset = slice_end  # jump past the oversized delta
                        self._tail_pending = b""
                        slice_cap_fired = True
                    dur_ms = int((time.monotonic() - t0) * 1000)
                    if dur_ms >= _REALTIME_TIMING_LOG_MS or len(data) >= 100_000:
                        log.info("realtime tail read chat=%s bytes=%d lines=%d dur_ms=%d",
                                 self.chat_id, len(data), len(lines), dur_ms)
                    # Track earliest suppressed entry offset so we don't
                    # advance the tail cursor past entries that were never
                    # forwarded. Without this, bot-side query timeouts cause
                    # permanent data loss: the daemon suppresses Desktop
                    # turns while queries are pending, but advances the
                    # offset past them — on respawn, those turns are behind
                    # the cursor and never appear in catchup.
                    first_suppressed_offset: int | None = None
                    for line_offset, line_bytes in lines:
                        if len(line_bytes) > _REALTIME_LINE_MAX_BYTES:
                            continue
                        try:
                            entry = json.loads(line_bytes)
                        except Exception:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        # D: skip API error turns — SDK/transport noise,
                        # not real Desktop activity.
                        # M3: log at debug for audit trail.
                        if entry.get("isApiErrorMessage") is True:
                            log.debug("realtime tail skip isApiErrorMessage type=%s",
                                      entry.get("type"))
                            continue
                        role = entry.get("type")
                        if role not in ("user", "assistant"):
                            continue
                        msg = entry.get("message") or {}
                        mid = msg.get("id") if isinstance(msg, dict) else None
                        # Dedup order: id-first, range-second.
                        if isinstance(mid, str) and mid:
                            if mid in self._own_message_id_set:
                                continue
                            # id present, not ours → DEFINITELY Desktop.
                        else:
                            if self._is_own_write_offset(line_offset):
                                continue
                        text = _extract_text(entry)
                        if not text:
                            continue
                        # Suppress desktop_turn while a bot query is in-flight.
                        # Own-write dedup (id set + offset range) has a timing gap
                        # on fresh spawn: CLI writes before ranges are fully
                        # registered. Suppressing during active queries is the
                        # belt-and-suspenders fix — own entries are already
                        # streamed to TG via the query's stream events.
                        if self._pending_qids:
                            log.debug("realtime tail suppressed (query active): "
                                      "role=%s bytes=%d qids=%d",
                                      role, len(text), len(self._pending_qids))
                            # DON'T advance offset past this entry — if the
                            # bot-side query times out, the response was never
                            # delivered. Preserving the offset here ensures
                            # catchup on daemon respawn will include it.
                            if first_suppressed_offset is None:
                                first_suppressed_offset = line_offset
                            continue
                        self._tail_seq += 1
                        self._realtime_turns_forwarded += 1
                        truncated = text[:_CATCHUP_TRUNCATE_CHARS]
                        files = _extract_files(entry) if role == "assistant" else []
                        dt_evt: dict = {
                            "type": "desktop_turn",
                            "chat_id": self.chat_id,
                            "uuid": self.uuid,
                            "seq": self._tail_seq,
                            "role": role,
                            "text": truncated,
                        }
                        # Include message_id for bot-side crash-recovery
                        # dedup (idempotent consumer pattern).
                        if mid:
                            dt_evt["message_id"] = mid
                        if files:
                            dt_evt["files"] = files
                        _emit(dt_evt)
                        log.info("realtime tail emit chat=%s seq=%d role=%s bytes=%d",
                                 self.chat_id, self._tail_seq, role, len(text))
                    # Cap offset: don't advance past suppressed entries.
                    # Exception: if the slice-cap safety valve fired, don't
                    # override it — the slice cap prevents unbounded re-reads
                    # when the JSONL grows while queries are stuck.
                    if (first_suppressed_offset is not None
                            and not slice_cap_fired):
                        capped = min(new_offset, first_suppressed_offset)
                        if capped < new_offset:
                            # Offset is rewinding — discard _tail_pending to
                            # avoid stale trailing bytes corrupting the next
                            # tick's combined_base calculation.
                            self._tail_pending = b""
                            log.debug("realtime tail offset capped at "
                                      "suppressed entry chat=%s offset=%d",
                                      self.chat_id, capped)
                        new_offset = capped
                    current_offset = new_offset
                    _save_tail_offset(self.chat_id, self.uuid, current_offset)
            except asyncio.CancelledError:
                log.info("realtime tail cancelled chat=%s", self.chat_id)
                return
            except Exception as e:
                log.warning("realtime tail error chat=%s: %s", self.chat_id, e)
            await asyncio.sleep(_REALTIME_TAIL_INTERVAL_S)


# ─────────────────────────────────────────────────────────────────────────
# Main event loop
# ─────────────────────────────────────────────────────────────────────────

async def _heartbeat_loop(session: _DaemonSession) -> None:
    """Emit periodic heartbeat and persist state. Exits on idle timeout."""
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            state = "busy" if session.current_query_id else "idle"
            _emit({"type": "heartbeat", "ts": int(time.time()), "state": state})
            session._persist_state(state)
            # Idle-exit check
            if not session.current_query_id:
                if time.time() - session.last_activity > _IDLE_TIMEOUT_S:
                    log.info("Idle timeout reached — exiting cleanly")
                    _emit({"type": "exit", "reason": "idle_timeout"})
                    os._exit(0)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("heartbeat error: %s", e)


async def _stdin_loop(session: _DaemonSession) -> None:
    """Read line-delimited JSON from stdin, dispatch queries."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=_MAX_QUERY_BYTES * 2)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
        except (asyncio.IncompleteReadError, ValueError) as e:
            log.warning("stdin read error: %s", e)
            break
        if not line:
            log.info("stdin EOF — exiting")
            break
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception as e:
            _emit({"type": "error", "error": f"bad_stdin_json:{e}"})
            continue
        mtype = msg.get("type") if isinstance(msg, dict) else None

        if mtype == "ping":
            _emit({"type": "pong", "ts": int(time.time())})
        elif mtype == "quit":
            log.info("quit received")
            _emit({"type": "exit", "reason": "quit_command"})
            break
        elif mtype == "query":
            qid = str(msg.get("id") or "")
            text = msg.get("text") or ""
            if not qid or not text:
                _emit({"type": "error", "query_id": qid,
                       "error": "missing_id_or_text"})
                continue
            if len(text.encode("utf-8")) > _MAX_QUERY_BYTES:
                _emit({"type": "error", "query_id": qid,
                       "error": "query_too_large"})
                continue
            # Run query in background so stdin loop stays responsive (allows
            # future cancel/ping during long queries)
            asyncio.create_task(session.run_query(qid, text))
        else:
            _emit({"type": "error", "error": f"unknown_type:{mtype}"})


async def _async_main(chat_id: str, uuid: str, cwd: str) -> int:
    session = _DaemonSession(chat_id=chat_id, uuid=uuid, cwd=cwd)

    is_new = (uuid == _NEW_SESSION)
    # Priority: env override (bot-side per-relay setting) > JSONL auto-detect
    env_model = os.environ.get("RELAY_MODEL_OVERRIDE", "")
    if env_model:
        detected_model = env_model
    elif not is_new:
        detected_model = _detect_model_from_jsonl(uuid)
    else:
        detected_model = None
    session.detected_model = detected_model
    settings = _detect_settings(cwd)
    session.detected_settings = settings
    _emit({
        "type": "ready",
        "chat_id": chat_id,
        "uuid": uuid,
        "pid": os.getpid(),
        "resumed": not is_new,
        "detected_model": detected_model,
        "effort": settings.get("effort"),
        "fresh_session": is_new,
    })
    log.info("daemon ready chat=%s model=%s effort=%s fresh=%s",
             chat_id, detected_model or "(default)",
             settings.get("effort"), is_new)
    session.mark_idle()

    hb_task = asyncio.create_task(_heartbeat_loop(session))
    tail_task = asyncio.create_task(session._realtime_tail_loop())
    try:
        await _stdin_loop(session)
    finally:
        for t in (hb_task, tail_task):
            t.cancel()
        for t in (hb_task, tail_task):
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except Exception:
                pass
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        _emit({"type": "error",
               "error": f"usage:sdk_persistent_daemon.py <chat_id> <uuid> <cwd_b64> (got {len(sys.argv)-1})"})
        return 2

    chat_id = _sanitise_chat_id(sys.argv[1])
    uuid = sys.argv[2]
    cwd_b64 = sys.argv[3]

    if uuid != _NEW_SESSION and not _UUID_RE.match(uuid):
        _emit({"type": "error", "error": f"invalid_uuid:{uuid[:40]}"})
        return 2

    try:
        cwd_bytes = base64.b64decode(cwd_b64, validate=True)
    except Exception as e:
        _emit({"type": "error", "error": f"cwd_b64_decode:{e}"})
        return 2
    if len(cwd_bytes) > _MAX_CWD_BYTES:
        _emit({"type": "error", "error": "cwd_too_large"})
        return 2
    try:
        cwd = cwd_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _emit({"type": "error", "error": "cwd_not_utf8"})
        return 2
    if not cwd.startswith("/"):
        _emit({"type": "error", "error": f"bad_cwd:{cwd[:80]}"})
        return 2
    if not Path(cwd).is_dir():
        try:
            Path(cwd).mkdir(parents=True, exist_ok=True)
            log.info("auto-created missing cwd: %s", cwd)
        except OSError as _mke:
            _emit({"type": "error", "error": f"mkdir_failed:{cwd[:80]}:{_mke}"})
            return 2

    _setup_logging(chat_id)
    log.info("=== sdk_persistent_daemon start === chat=%s uuid=%s cwd=%s pid=%d",
             chat_id, uuid, cwd, os.getpid())

    try:
        _check_or_claim_pidfile(chat_id)
    except SystemExit as e:
        return int(getattr(e, "code", 3) or 3)

    # Graceful shutdown on SIGTERM/SIGHUP → signal stdin loop via exit
    def _sig(signum, _frame):
        log.info("Signal %d received — exiting", signum)
        _emit({"type": "exit", "reason": f"signal_{signum}"})
        _cleanup_pidfile(chat_id)
        os._exit(0)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGHUP, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        rc = asyncio.run(_async_main(chat_id, uuid, cwd))
    finally:
        _cleanup_pidfile(chat_id)
    return rc


if __name__ == "__main__":
    sys.exit(main())
