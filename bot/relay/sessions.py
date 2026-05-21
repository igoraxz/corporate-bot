# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Daemon session management for employee workstation control.

Functions for listing/killing persistent SDK daemons on employee Macs,
toggling workstation channel mode, and getting employee Mac process snapshots.
"""

import logging
import os
import time

log = logging.getLogger(__name__)

__all__ = [
    "list_daemons",
    "kill_daemon",
    "reap_orphans",
    "set_relay_mode",
    "get_relay_mode",
    "get_mac_process_snapshot",
]


async def list_daemons() -> dict:
    """List all persistent SDK daemons on Mac via guard.sh relay-list-daemons.

    guard.sh emits one line per daemon (7 fields, last 2 optional):
        <chat_id>  <pid>  <status>  <heartbeat_age>s  <uuid>  [<model>]  [<effort>]
    or the sentinel `NO_DAEMONS` / `NO_DAEMONS_DIR`.

    Returns dict: {ok, daemons: [{chat_id, pid, status, heartbeat_age_s, uuid, model, effort}], raw}
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    ok, output = await execute("relay-list-daemons", timeout=10.0)
    if not ok:
        # guard.sh may exit non-zero but still have valid daemon data
        # (e.g. [ "$FOUND" -eq 0 ] short-circuit leaves exit code 1).
        # Try parsing before giving up — look for ALIVE/DEAD markers.
        out = output.strip()
        if "ALIVE" not in out and "DEAD" not in out:
            return {"ok": False, "daemons": [], "error": output, "raw": output}
        log.warning("relay-list-daemons: non-zero exit but output looks valid, parsing anyway")
        # Fall through to normal parsing below
    else:
        out = output.strip()
    if out in ("NO_DAEMONS", "NO_DAEMONS_DIR", ""):
        return {"ok": True, "daemons": [], "raw": out}
    daemons: list[dict] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        chat_id, pid, status, hb_raw, uid = parts[0], parts[1], parts[2], parts[3], parts[4]
        model = parts[5] if len(parts) > 5 else None
        effort = parts[6] if len(parts) > 6 else None
        # Normalize "-" sentinel to None
        if model == "-":
            model = None
        if effort == "-":
            effort = None
        hb_s: int | None
        try:
            hb_s = int(hb_raw.rstrip("s"))
        except Exception:
            hb_s = None
        daemons.append({
            "chat_id": chat_id,
            "pid": pid,
            "status": status,
            "heartbeat_age_s": hb_s,
            "uuid": uid,
            "model": model,
            "effort": effort,
        })
    return {"ok": True, "daemons": daemons, "raw": out}


async def kill_daemon(chat_id: str) -> dict:
    """TERM→KILL a specific chat's persistent daemon via guard.sh relay-kill-daemon.

    Returns dict: {ok, killed_pid, raw}
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    if not chat_id or not chat_id.lstrip("-").isdigit():
        return {"ok": False, "error": "invalid_chat_id"}
    ok, output = await execute(f"relay-kill-daemon {chat_id}", timeout=15.0)
    raw = output.strip()
    if ok and raw.startswith("KILLED:"):
        return {"ok": True, "killed_pid": raw.split(":", 1)[1], "raw": raw}
    return {"ok": False, "error": raw, "raw": raw}


async def reap_orphans() -> dict:
    """Sweep dead daemon pidfiles via guard.sh relay-reap-orphans.

    Returns dict: {ok, reaped_count, raw}
    """
    from bot.relay.ssh import execute  # noqa: PLC0415

    ok, output = await execute("relay-reap-orphans", timeout=15.0)
    if not ok:
        return {"ok": False, "error": output, "raw": output}
    raw = output.strip()
    # guard.sh emits `REAPED:<count>` or `NO_DAEMONS_DIR`.
    reaped = 0
    for line in raw.splitlines():
        if line.startswith("REAPED:"):
            try:
                reaped = int(line.split(":", 1)[1])
            except Exception:
                pass
    return {"ok": True, "reaped_count": reaped, "raw": raw}


async def set_relay_mode(chat_id: str, enabled: bool) -> dict:
    """Enable or disable relay mode for a chat.

    When relay_mode is on, messages in this chat bypass the bot's SDK and go
    directly to a persistent daemon on Mac. Zero token cost per message.
    """
    from bot.relay.registry import (  # noqa: PLC0415
        _registry_lock, _load_registry, _save_registry,
    )

    async with _registry_lock:
        registry = _load_registry()
        entry = registry.get(str(chat_id))
        if not entry:
            return {"success": False, "error": f"No linked session for chat_id={chat_id}. Link first."}
        entry["relay_mode"] = enabled
        _save_registry(registry)

    log.info("relay_mode %s: chat_id=%s session=%s",
             "ENABLED" if enabled else "DISABLED", chat_id, entry.get("session_name"))
    return {"success": True, "relay_mode": enabled, "session_name": entry.get("session_name")}


def get_relay_mode(chat_id: str) -> dict | None:
    """Check if a chat has relay_mode enabled. Returns entry or None.

    Synchronous read — no lock needed (atomic rename in _save_registry).
    """
    from bot.relay.registry import _load_registry  # noqa: PLC0415

    registry = _load_registry()
    entry = registry.get(str(chat_id))
    if entry and entry.get("relay_mode"):
        return entry
    return None


async def get_mac_process_snapshot() -> dict:
    """Get a snapshot of Mac processes spawned by bot automation.

    Returns dict with:
      - bot_ssh_procs: count of tracked SSH subprocesses still alive
      - bot_ssh_stale: list of SSH procs older than 30min (potential orphans)
      - mac_daemons: output from relay-list-daemons (if authed)
      - mac_daemon_count: number of active daemons
    """
    from bot.relay.ssh import _ssh_procs  # noqa: PLC0415
    import bot.relay.core as _core  # noqa: PLC0415 — module ref for mutable state

    now = time.time()
    stale_threshold = 1800  # 30 minutes

    # Bot-side: check tracked SSH processes
    alive = 0
    stale: list[dict] = []
    dead_pids: list[int] = []
    for pid, (spawn_t, desc) in _ssh_procs.items():
        # Check if process is still running
        try:
            os.kill(pid, 0)  # signal 0 = check existence
            alive += 1
            age = now - spawn_t
            if age > stale_threshold:
                stale.append({"pid": pid, "age_min": int(age / 60), "cmd": desc})
        except (OSError, ProcessLookupError):
            dead_pids.append(pid)
    # Clean up dead entries
    for pid in dead_pids:
        _ssh_procs.pop(pid, None)

    # Mac-side: list daemons (reuse list_daemons() for resilient parsing)
    mac_daemons = ""
    mac_daemon_count = 0
    if _core._cached_authenticated:
        result = await list_daemons()
        if result.get("ok"):
            mac_daemons = result.get("raw", "")
            mac_daemon_count = len(result.get("daemons", []))

    return {
        "bot_ssh_procs": alive,
        "bot_ssh_stale": stale,
        "bot_ssh_tracked_total": len(_ssh_procs),
        "mac_daemons": mac_daemons,
        "mac_daemon_count": mac_daemon_count,
    }
