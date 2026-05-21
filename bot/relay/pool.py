# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Persistent employee workstation SDK client pool.

Mirrors agent.py ClientPool but for employee Mac-side workstation daemons. One
long-lived SSH+daemon pair per workstation channel. Queries are multiplexed
over stdin/stdout with correlation ids (line-delimited JSON).

State machine per client:

    DISCONNECTED ──► CONNECTING ──► READY ⇄ BUSY
         ▲                │              │
         │                ▼              ▼
         └── RETRY ──── ERROR ◄──────────┘
                 ▲
                 │ exp backoff 1s/3s/9s/27s/60s, max 5 tries
                 ▼
                 DEAD (manual reset via MCP action)

Lifecycle:
  - `acquire(chat_id, uuid, cwd)` — get-or-create a client, ensures ready
  - `client.query(text)` — async generator yielding stream entries
  - `release(chat_id)` — graceful quit + pool eviction
  - `shutdown_all()` — on bot shutdown

Transport: one `ssh -T` subprocess per chat, kept alive via ServerAliveInterval.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid as _uuidlib
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import AsyncIterator


# Module-level forwarder for Desktop-typed turns → TG. Set by main.py at
# lifespan startup to avoid circular imports (main.py has TG send helpers;
# pool can't import from main). Each client spawns a listener task that
# drains its `_desktop_turn_queue` and invokes this callback.
_desktop_turn_forwarder: Callable[[str, dict], Awaitable[None]] | None = None

# Catchup events (`catchup` / `catchup_warning`) arrive with
# query_id="attach" from the daemon's one-shot _emit_desktop_catchup path —
# they carry NO matching query_queue. Route them unsolicited through a
# dedicated queue + forwarder instead (same pattern as desktop_turn).
_catchup_forwarder: Callable[[str, dict], Awaitable[None]] | None = None


def set_desktop_turn_forwarder(fn: Callable[[str, dict], Awaitable[None]]) -> None:
    """Register the async callable that forwards Desktop turns to TG.
    Called once at bot startup from main.py.lifespan."""
    global _desktop_turn_forwarder
    _desktop_turn_forwarder = fn


def set_catchup_forwarder(fn: Callable[[str, dict], Awaitable[None]]) -> None:
    """Register the async callable that forwards catchup events to TG.
    Called once at bot startup from main.py.lifespan."""
    global _catchup_forwarder
    _catchup_forwarder = fn

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────

DESKTOP_ENABLED = os.environ.get("DESKTOP_ENABLED", "false").lower() == "true"
DESKTOP_HOST = os.environ.get("DESKTOP_HOST", "your-mac")
DESKTOP_USER = os.environ.get("DESKTOP_USER", "user")
_SSH_CONFIG_DIR = os.path.expanduser("~/.ssh-config")


def _ssh_cmd_base(mac_host: str = "", mac_user: str = "") -> list[str]:
    """SSH command prefix with dynamically-resolved Mac IP. Resolution
    chain lives in bot/relay/mac_resolver.py (env pin → Tailscale API → DNS →
    disk cache → hardcoded default). Called at each spawn so the latest
    cached IP is used without restart.

    CB-140: Accepts optional per-user host/user overrides from WorkstationConfig.
    Falls back to global DESKTOP_HOST/DESKTOP_USER for backward compat.
    """
    _host = mac_host or DESKTOP_HOST
    _user = mac_user or DESKTOP_USER
    from bot.relay.mac_resolver import get_ssh_host_opts  # noqa: PLC0415
    return [
        "/usr/bin/ssh",
        "-F", f"{_SSH_CONFIG_DIR}/config",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
        *get_ssh_host_opts(hostname_override=_host),
        f"{_user}@{_host}",
    ]

_STREAM_LIMIT = 10 * 1024 * 1024  # 10MB line buffer — matches bot-side fix
_READY_TIMEOUT_S = 15.0
_QUERY_WALL_TIMEOUT_S = 10800.0  # 3h hard wall-clock cap
_QUERY_IDLE_TIMEOUT_S = 2700.0   # 45 min idle (no events from daemon)
_QUIT_TIMEOUT_S = 5.0
_RETRY_BACKOFF = [1, 3, 9, 27, 60]  # seconds, then DEAD


# ─────────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────────

class ClientState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"
    RETRY = "retry"
    DEAD = "dead"


# ─────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────

class RelayPersistentClient:
    """One persistent daemon connection per relay chat."""

    def __init__(self, chat_id: str, uuid: str, cwd: str):
        self.chat_id = chat_id
        self.uuid = uuid
        self.cwd = cwd
        self.state: ClientState = ClientState.DISCONNECTED
        self._proc: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._query_locks: dict[str, asyncio.Event] = {}
        # For correlating entries to queries — each query_id maps to an asyncio.Queue
        self._query_queues: dict[str, asyncio.Queue] = {}
        self._reader_task: asyncio.Task | None = None
        self._retry_count = 0
        self._last_heartbeat = 0.0
        self._ready_event: asyncio.Event = asyncio.Event()
        self._connect_lock = asyncio.Lock()
        # Realtime Desktop→TG catchup (item 2). Daemon emits `desktop_turn`
        # events for each Desktop-typed turn observed in the pinned JSONL.
        # Dispatched by _reader_loop → consumed by main.py listener task.
        # Bounded to prevent unbounded memory under SSH reconnect / slow
        # consumer. On overflow we drop-newest (put_nowait raises QueueFull).
        self._desktop_turn_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._turn_listener_task: asyncio.Task | None = None
        # Captures non-JSON lines from daemon stdout (e.g. guard.sh auth
        # rejection text). Checked on ready-timeout to surface the real cause.
        self._non_json_output: list[str] = []

    # ─── connect / disconnect ───────────────────────────────────────────

    async def ensure_ready(self) -> None:
        """Connect if not connected; wait for ready. Idempotent."""
        async with self._connect_lock:
            if self.state in (ClientState.READY, ClientState.BUSY):
                return
            if self.state == ClientState.DEAD:
                raise RuntimeError(f"client for chat={self.chat_id} is DEAD; manual reset required")
            await self._spawn()

    async def _spawn(self) -> None:
        self.state = ClientState.CONNECTING
        self._ready_event.clear()
        self._non_json_output.clear()
        cwd_b64 = base64.b64encode(self.cwd.encode("utf-8")).decode("ascii")
        # Look up registry entry for account_label and relay overrides
        # (model, effort, context_1m). Pass as positional args to guard.sh.
        label = ""
        model_arg = "-"
        effort_arg = "-"
        try:
            from bot.desktop_relay import _load_registry, validate_account_label
            entry = _load_registry().get(str(self.chat_id), {})
            lbl = entry.get("account_label", "")
            if lbl and validate_account_label(lbl):
                label = lbl
            # Relay overrides: model_override, effort_override, context_1m
            # context_1m appends [1m] suffix to model name for 1M context window.
            # Requires model_override to be set — if not, daemon explicitly adds
            # from JSONL (raw API names never have [1m]) and context_1m has
            # no effect. The /session context command warns about this.
            m_ovr = entry.get("model_override", "")
            if m_ovr:
                if entry.get("context_1m") and "[1m]" not in m_ovr:
                    m_ovr = f"{m_ovr}[1m]"
                model_arg = m_ovr
            effort_ovr = entry.get("effort_override", "")
            if effort_ovr:
                effort_arg = effort_ovr
        except Exception as _le:
            log.debug("pool: registry lookup failed chat=%s: %s",
                      self.chat_id, _le)
        cmd = f"relay-sdk-persistent {self.chat_id} {self.uuid} {cwd_b64} {label or '-'} {model_arg} {effort_arg}"
        # CB-140: Resolve per-user workstation config for SSH target
        _ws_host, _ws_user = "", ""
        try:
            from bot.relay.workstation_config import get_workstation_for_chat
            _ws_uid, _ws_cfg = get_workstation_for_chat(str(self.chat_id))
            if _ws_cfg and _ws_cfg.mac_host and _ws_cfg.mac_user:
                _ws_host, _ws_user = _ws_cfg.mac_host, _ws_cfg.mac_user
                log.info("Relay pool: using per-user workstation for chat=%s user=%s host=%s",
                         self.chat_id, _ws_uid, _ws_host)
        except Exception as _ws_err:
            log.debug("Relay pool: workstation config lookup failed: %s", _ws_err)
        log.info("Relay pool: spawning daemon for chat=%s uuid=%s",
                 self.chat_id, self.uuid)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *_ssh_cmd_base(mac_host=_ws_host, mac_user=_ws_user), cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
            )
        except FileNotFoundError:
            self.state = ClientState.ERROR
            raise RuntimeError("SSH binary not found")
        except Exception as e:
            self.state = ClientState.ERROR
            raise RuntimeError(f"spawn_failed:{e}")

        # Background task reads stdout, dispatches entries to per-query queues
        self._reader_task = asyncio.create_task(self._reader_loop())

        # Wait for "ready" message
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=_READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Before generic timeout, check if guard.sh rejected due to auth.
            # Collect non-JSON stdout (captured by _reader_loop) + stderr.
            hints = list(self._non_json_output)
            if self._proc and self._proc.stderr:
                try:
                    stderr_bytes = await asyncio.wait_for(
                        self._proc.stderr.read(4096), timeout=1.0)
                    if stderr_bytes:
                        hints.append(stderr_bytes.decode("utf-8", errors="replace").strip())
                except Exception:
                    pass
            hint_text = " | ".join(hints)[:500]
            await self._force_kill()
            self.state = ClientState.ERROR
            # Detect guard.sh auth rejection patterns
            ht = hint_text.lower()
            if "auth_required" in ht or "no_session" in ht or "session_expired" in ht:
                raise RuntimeError(f"auth_expired:{hint_text}")
            raise RuntimeError(f"daemon_ready_timeout:{hint_text}" if hint_text
                               else "daemon_ready_timeout")
        self._retry_count = 0

        # Start realtime-turn listener task — drains the queue and invokes
        # the module-level forwarder (set by main.py). Started AFTER ready
        # so we never drain before the daemon emits anything. If forwarder
        # is None (early boot / misconfig), listener idles safely.
        if self._turn_listener_task is None or self._turn_listener_task.done():
            self._turn_listener_task = asyncio.create_task(
                self._turn_listener_loop())

    async def _turn_listener_loop(self) -> None:
        """Drain desktop_turn_queue and forward to TG via module callback.

        Retries forever (until client is DEAD or task cancelled). Forwarder
        exceptions are caught so one bad TG send never breaks the listener.
        """
        log.info("turn listener start chat=%s", self.chat_id)
        try:
            while self.state not in (ClientState.DEAD,):
                evt = await self.next_desktop_turn(timeout=5.0)
                if evt is None:
                    continue
                fn = _desktop_turn_forwarder
                if fn is None:
                    # Forwarder not yet registered — re-enqueue + wait briefly.
                    try:
                        self._desktop_turn_queue.put_nowait(evt)
                    except asyncio.QueueFull:
                        log.warning(
                            "turn listener chat=%s forwarder missing "
                            "and queue full — dropping seq=%s",
                            self.chat_id, evt.get("seq"))
                    await asyncio.sleep(2.0)
                    continue
                try:
                    await fn(self.chat_id, evt)
                except Exception as e:
                    log.warning("turn listener forward failed chat=%s seq=%s: %s",
                                self.chat_id, evt.get("seq"), e)
        except asyncio.CancelledError:
            log.info("turn listener cancelled chat=%s", self.chat_id)
            return
        except Exception as e:
            log.warning("turn listener fatal chat=%s: %s", self.chat_id, e)

    async def _reader_loop(self) -> None:
        """Consume daemon stdout → dispatch to per-query queues or control events."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                try:
                    line = await self._proc.stdout.readline()
                except (asyncio.IncompleteReadError, ValueError) as e:
                    log.warning("Relay pool reader error chat=%s: %s", self.chat_id, e)
                    break
                if not line:
                    break
                raw_line = line.decode("utf-8", errors="replace").strip()
                try:
                    msg = json.loads(raw_line)
                except Exception:
                    # Non-JSON output (e.g. guard.sh auth rejection). Capture
                    # for diagnosis on ready-timeout instead of silently dropping.
                    # Cap at 20 entries for defense-in-depth (guard.sh exits
                    # after 1-2 lines; this guards against pathological SSH output).
                    if raw_line and len(self._non_json_output) < 20:
                        self._non_json_output.append(raw_line)
                        log.debug("reader chat=%s non-json: %s",
                                  self.chat_id, raw_line[:300])
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                log.debug("reader chat=%s recv type=%s qid=%s msg=%s",
                          self.chat_id, mtype, msg.get("query_id"),
                          str(msg)[:300] if mtype == "error" else "")
                if mtype == "ready":
                    self.state = ClientState.READY
                    self._last_heartbeat = time.time()
                    self._ready_event.set()
                    log.info("Relay pool ready: chat=%s pid=%s model=%s",
                             self.chat_id, msg.get("pid"),
                             msg.get("detected_model", "(unknown)"))
                elif mtype == "heartbeat":
                    self._last_heartbeat = time.time()
                elif mtype == "pong":
                    self._last_heartbeat = time.time()
                elif mtype == "exit":
                    log.info("Relay pool daemon exited: chat=%s reason=%s",
                             self.chat_id, msg.get("reason"))
                    break
                elif mtype in ("stream", "result", "error", "catchup",
                               "catchup_warning"):
                    qid = msg.get("query_id")
                    q = self._query_queues.get(qid) if qid else None
                    if q is not None:
                        await q.put(msg)
                    elif mtype in ("catchup", "catchup_warning"):
                        # Unsolicited: one-shot attach catchup from
                        # _DaemonSession.__init__ has qid="attach" (no
                        # query queue). Route to forwarder so the TG
                        # catchup digest delivers even before the first
                        # user query arrives.
                        fn = _catchup_forwarder
                        if fn is not None:
                            try:
                                await fn(self.chat_id, msg)
                            except Exception as e:
                                log.warning("catchup forwarder failed chat=%s: %s",
                                            self.chat_id, e)
                        else:
                            log.info("catchup received chat=%s but forwarder "
                                     "not registered — dropping", self.chat_id)
                elif mtype == "new_session":
                    # Fresh session: daemon found a new JSONL after
                    # starting without --resume. Update registry + self.uuid.
                    new_uuid = msg.get("uuid", "")
                    new_path = msg.get("jsonl_path", "")
                    import re as _re
                    _uuid_pat = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
                    if (new_uuid and new_path
                            and _uuid_pat.match(new_uuid)
                            and new_path.endswith(".jsonl")
                            and "/.claude/projects/" in new_path):
                        try:
                            from bot.desktop_relay import update_pinned_jsonl
                            await update_pinned_jsonl(self.chat_id, new_path)
                            self.uuid = new_uuid  # only after successful write
                            log.info("new_session: updated registry chat=%s uuid=%s",
                                     self.chat_id, new_uuid)
                        except Exception as e:
                            log.warning("new_session: registry update failed chat=%s: %s",
                                        self.chat_id, e)
                    else:
                        log.warning("new_session: rejected invalid uuid=%s path=%s",
                                    new_uuid[:40] if new_uuid else "", new_path[:80] if new_path else "")
                    # Also route to query queue so the bot can log it
                    qid = msg.get("query_id")
                    q = self._query_queues.get(qid) if qid else None
                    if q is not None:
                        await q.put(msg)
                elif mtype == "desktop_turn":
                    # Unsolicited realtime catchup — no query_id association.
                    # Enqueue for main.py listener; drop-newest on overflow.
                    try:
                        self._desktop_turn_queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        log.warning("desktop_turn queue full chat=%s seq=%s — dropping",
                                    self.chat_id, msg.get("seq"))
                elif mtype == "desktop_turn_meta":
                    # Rotation / slice-skip / other tailer notices — log only.
                    log.info("desktop_turn_meta chat=%s subtype=%s",
                             self.chat_id, msg.get("subtype"))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("Relay pool reader fatal chat=%s: %s", self.chat_id, e, exc_info=True)
        finally:
            # Broadcast disconnect to all pending queries
            for q in self._query_queues.values():
                try:
                    q.put_nowait({"type": "error", "error": "daemon_disconnected"})
                except Exception:
                    pass
            if self.state not in (ClientState.DEAD,):
                self.state = ClientState.DISCONNECTED

    async def _force_kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except Exception:
                pass
        if self._turn_listener_task and not self._turn_listener_task.done():
            self._turn_listener_task.cancel()
            try:
                await self._turn_listener_task
            except Exception:
                pass
        self._proc = None
        self._reader_task = None
        self._turn_listener_task = None

    async def quit_graceful(self) -> None:
        """Send {type:quit}, wait for daemon exit, clean up."""
        if self._proc and self._proc.stdin and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.write(b'{"type":"quit"}\n')
                await self._proc.stdin.drain()
            except Exception:
                pass
        try:
            if self._proc:
                await asyncio.wait_for(self._proc.wait(), timeout=_QUIT_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self._force_kill()
        self.state = ClientState.DISCONNECTED

    async def _refresh_totp_heartbeat(self) -> None:
        """Fire-and-forget: refresh Mac TOTP session TTL via relay-heartbeat."""
        try:
            from bot.relay.ssh import execute
            ok, out = await execute("relay-heartbeat", timeout=5.0)
            if ok:
                log.debug("TOTP heartbeat refreshed for chat=%s: %s",
                          self.chat_id, out.strip())
            else:
                log.debug("TOTP heartbeat failed for chat=%s: %s",
                          self.chat_id, out.strip()[:100])
        except Exception as e:
            log.debug("TOTP heartbeat error for chat=%s: %s", self.chat_id, e)

    # ─── query ──────────────────────────────────────────────────────────

    async def query(self, text: str) -> AsyncIterator[dict]:
        """Send a query; yield entries (stream/result/error) as they arrive."""
        if self.state == ClientState.DEAD:
            yield {"type": "error", "error": "client_dead"}
            return

        # Connect if needed, retry on transient failure
        try:
            await self.ensure_ready()
        except Exception as e:
            async for entry in self._retry_and_query(text, first_error=str(e)):
                yield entry
            return

        qid = _uuidlib.uuid4().hex[:12]
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._query_queues[qid] = q
        self.state = ClientState.BUSY

        # Refresh TOTP session TTL — the daemon runs via exec (replaces guard.sh),
        # so subsequent queries on the existing SSH pipe don't trigger guard.sh
        # heartbeat. Without this, the session expires after 2h even while
        # actively handling queries. Fire-and-forget, never blocks the query.
        asyncio.create_task(self._refresh_totp_heartbeat())

        # Prepend relay prelude (TG formatting + RELAY_FILE rules) so Mac
        # Claude always gets the same instruction preamble the ephemeral
        # path used to inject. Kept at the front of the user message so the
        # `[USER_QUERY]` anchor still marks where the actual input begins.
        from bot.desktop_relay import _RELAY_QUERY_PRELUDE
        full_text = _RELAY_QUERY_PRELUDE + text
        payload = json.dumps({"type": "query", "id": qid, "text": full_text}) + "\n"
        log.info("query chat=%s qid=%s text_bytes=%d",
                 self.chat_id, qid, len(text))
        try:
            async with self._write_lock:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(payload.encode("utf-8"))
                await self._proc.stdin.drain()
        except Exception as e:
            self._query_queues.pop(qid, None)
            self.state = ClientState.ERROR
            async for entry in self._retry_and_query(text, first_error=f"send_failed:{e}"):
                yield entry
            return

        # Stream entries until result/error
        # Hybrid timeout: 3h wall-clock cap + 45min idle (no events)
        start = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - start
                wall_remaining = _QUERY_WALL_TIMEOUT_S - elapsed
                if wall_remaining <= 0:
                    yield {"type": "error", "error": "query_timeout_wall",
                           "detail": f"wall clock {_QUERY_WALL_TIMEOUT_S/3600:.0f}h exceeded"}
                    break
                idle_cap = min(_QUERY_IDLE_TIMEOUT_S, wall_remaining)
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=idle_cap)
                except asyncio.TimeoutError:
                    kind = "query_timeout_wall" if wall_remaining <= _QUERY_IDLE_TIMEOUT_S \
                        else "query_timeout_idle"
                    yield {"type": "error", "error": kind,
                           "detail": f"no events for {_QUERY_IDLE_TIMEOUT_S/60:.0f}min"
                                     if kind == "query_timeout_idle"
                                     else f"wall clock {_QUERY_WALL_TIMEOUT_S/3600:.0f}h exceeded"}
                    break
                log.debug("query chat=%s qid=%s yield type=%s",
                          self.chat_id, qid, entry.get("type"))
                yield entry
                if entry.get("type") in ("result", "error"):
                    break
        finally:
            self._query_queues.pop(qid, None)
            if self.state != ClientState.DISCONNECTED:
                self.state = ClientState.READY

    async def _retry_and_query(self, text: str, first_error: str) -> AsyncIterator[dict]:
        """Backoff + retry chain. Yields a single error entry on final DEAD."""
        log.warning("Relay pool retry chat=%s err=%s", self.chat_id, first_error)
        # Auth errors: skip retries entirely — TOTP won't magically refresh.
        if first_error.startswith("auth_expired:"):
            self.state = ClientState.DEAD
            yield {"type": "error", "error": first_error}
            return
        if self._retry_count >= len(_RETRY_BACKOFF):
            self.state = ClientState.DEAD
            yield {"type": "error",
                   "error": f"client_dead_after_retries:{first_error}"}
            return
        await self._force_kill()
        self.state = ClientState.RETRY
        delay = _RETRY_BACKOFF[self._retry_count]
        self._retry_count += 1
        await asyncio.sleep(delay)
        # One retry attempt — yield its output
        try:
            async for entry in self.query(text):
                yield entry
        except Exception as e:
            yield {"type": "error", "error": f"retry_failed:{e}"}

    # ─── introspection ──────────────────────────────────────────────────

    def health(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "uuid": self.uuid,
            "state": self.state.value,
            "retry_count": self._retry_count,
            "heartbeat_age_s": (
                time.time() - self._last_heartbeat if self._last_heartbeat else None),
            "pid": self._proc.pid if self._proc and self._proc.pid else None,
            "pending_queries": len(self._query_queues),
            "desktop_turns_pending": self._desktop_turn_queue.qsize(),
        }

    async def next_desktop_turn(self, timeout: float = 5.0) -> dict | None:
        """Wait for the next Desktop-typed turn or timeout.

        Public accessor so callers don't touch the private queue directly.
        Returns None on timeout (caller polls again). Never raises.
        """
        try:
            return await asyncio.wait_for(
                self._desktop_turn_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            log.warning("next_desktop_turn chat=%s error: %s", self.chat_id, e)
            return None


# ─────────────────────────────────────────────────────────────────────────
# Pool
# ─────────────────────────────────────────────────────────────────────────

class RelayClientPool:
    """Maps chat_id → RelayPersistentClient."""

    def __init__(self):
        self._clients: dict[str, RelayPersistentClient] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, chat_id: str, uuid: str, cwd: str) -> RelayPersistentClient:
        async with self._lock:
            existing = self._clients.get(chat_id)
            if existing:
                if existing.uuid != uuid:
                    # Chat repinned to different UUID — retire old client
                    log.info("Relay pool uuid changed chat=%s old=%s new=%s",
                             chat_id, existing.uuid, uuid)
                    await existing.quit_graceful()
                    self._clients.pop(chat_id, None)
                elif existing.state != ClientState.DEAD:
                    return existing
                else:
                    self._clients.pop(chat_id, None)
            client = RelayPersistentClient(chat_id, uuid, cwd)
            self._clients[chat_id] = client
            return client

    async def release(self, chat_id: str) -> None:
        async with self._lock:
            client = self._clients.pop(chat_id, None)
        if client:
            await client.quit_graceful()

    async def shutdown_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for c in clients:
            try:
                await c.quit_graceful()
            except Exception:
                pass

    def health_all(self) -> list[dict]:
        return [c.health() for c in self._clients.values()]

    def get_client(self, chat_id: str) -> RelayPersistentClient | None:
        """Non-mutating lookup — does NOT spawn. Returns None if no client.
        Used by realtime Desktop-turn listener (main.py) to attach without
        forcing daemon spawn on cold chats (SEC-8 fix).
        """
        return self._clients.get(chat_id)


# Module-level singleton (bot-wide). Created lazily on first use.
_pool: RelayClientPool | None = None


def get_pool() -> RelayClientPool:
    global _pool
    if _pool is None:
        _pool = RelayClientPool()
    return _pool
