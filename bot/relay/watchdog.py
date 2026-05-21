# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Employee Mac health monitoring — watchdog, daemon respawn, workstation channel alerts.

Monitors employee Mac connectivity and authentication state, handles offline→online
transitions with catchup, respawns unhealthy daemons, and alerts workstation channels.
"""

import asyncio
import logging
import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.relay.pool import RelayClientPool

log = logging.getLogger(__name__)

__all__ = [
    "mac_health_watchdog",
    "_kick_one_chat",
    "_kick_relay_daemons_for_post_online_catchup",
    "_eager_respawn_after_kick",
    "_eager_respawn_one",
    "_respawn_unhealthy_daemons",
    "_alert_relay_chats",
    "_mac_was_online",
    "_mac_offline_since",
    "_mac_alerted",
    "_ever_seen_online",
    "_mac_was_authenticated",
    "_kick_lock",
    "_respawn_last_attempt",
    "_RESPAWN_COOLDOWN_S",
    "_active_relay_chats",
    "_relay_tasks",
]

# --- Phase 7: Mac health watchdog + catch-up sync ---
_mac_was_online: bool = False
_mac_offline_since: float = 0.0
# Track chats with active relay polling loops (to avoid background sync conflict)
_active_relay_chats: set[str] = set()
# Track active relay asyncio.Task per chat_id — cancelled on relay disable/delete
_relay_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
_mac_alerted: bool = False  # prevents repeated offline alerts
# Flips True on FIRST successful ping this process and never resets.
# Suppresses the cold-offline "went offline" alert when the bot starts with Mac
# already offline (no real transition). Without this, every container restart
# fires a spurious "went offline" to relay chats because _mac_alerted is
# in-memory and reset on boot. See mac_health_watchdog cold-offline branch.
_ever_seen_online: bool = False
_mac_was_authenticated: bool = False
# How often (in watchdog ticks) to refresh auth status from Mac via status()
_AUTH_REFRESH_INTERVAL_S: float = 120.0

# Serializes the post-online kick cascade so that overlapping watchdog ticks
# (e.g., scheduler hiccup, manual triggers) cannot fire two in parallel and
# double-kill daemons mid-respawn. Held only during the kick — fast lock.
_kick_lock: asyncio.Lock = asyncio.Lock()
_PER_CHAT_KICK_TIMEOUT_S: float = 8.0
_POOL_RELEASE_TIMEOUT_S: float = 3.0
_MAC_OFFLINE_KICK_THRESHOLD_S: float = 60.0

# Per-chat respawn cooldown to prevent infinite respawn loops for
# persistently failing daemons (bad UUID, deleted project, etc.).
_RESPAWN_COOLDOWN_S: float = 300.0  # 5 minutes
_respawn_last_attempt: dict[str, float] = {}  # chat_id → monotonic timestamp


async def mac_health_watchdog():
    """Check Mac connectivity and alert/sync relay chats on state transitions.

    Called from main.py scheduler tick loop every ~5 min.
    - offline→online: catch-up sync (JSONL diff) + notification
    - online→offline (>5min): one-shot offline alert to relay chats
    - cold-offline (bot starts with Mac unreachable): alert SUPPRESSED until
      we've seen Mac online at least once this process (see _ever_seen_online)
    """
    global _mac_was_online, _mac_offline_since, _mac_alerted, _ever_seen_online

    import bot.relay.core as _core  # noqa: PLC0415 — module ref for mutable state
    from bot.relay.core import DESKTOP_ENABLED, get_cached_status  # noqa: PLC0415
    from bot.relay.core import ping, status  # noqa: PLC0415
    from bot.relay.sessions import get_mac_process_snapshot  # noqa: PLC0415
    from bot.relay.ssh import execute  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return

    # Refresh the dynamic Mac IP cache before anything else — a stale IP
    # would cause ping() to fail on the wrong target. bot.relay.mac_resolver
    # honors its own TTL so this is cheap (skip if within MAC_IP_TTL_S).
    try:
        from bot.relay.mac_resolver import refresh_mac_ip  # noqa: PLC0415
        await refresh_mac_ip()
    except Exception as e:
        log.debug("[Mac watchdog] IP resolver refresh failed: %s", e)

    # Check Mac connectivity via relay-ping (exempt from TOTP auth)
    result = await ping()
    ok = result.get("online", False)

    # On first successful ping after startup, do a full status() to cache auth state
    if ok and _core._cached_auth_time == 0:
        log.info("[Mac watchdog] First ping OK — checking auth status")
        try:
            await status()  # caches authenticated + session_remaining
        except Exception as e:
            log.warning("[Mac watchdog] Auth check failed: %s", e)

    if ok and not _mac_was_online:
        # Transition: offline → online
        was_offline_for = (time.time() - _mac_offline_since) if _mac_offline_since else 0.0
        _mac_was_online = True
        _mac_offline_since = 0.0
        _mac_alerted = False
        _ever_seen_online = True  # unlock cold-offline alert for future drops
        log.info("[Mac watchdog] Mac came online (was_offline_for=%.0fs)",
                 was_offline_for)
        # Trigger post-online catchup for each relay chat:
        # If Mac was offline >60s, force-kill any live daemon on Mac and
        # release the pool client. Next user message respawns daemon, whose
        # __init__ emits a "delta" catchup of last 10 turns since last sync.
        # Bot tags chat as recently_recovered so the catchup header reads
        # "Mac back online — last 10 new turns from Desktop".
        # Skip the kill cascade for short blips (<60s) to avoid spam.
        if was_offline_for >= _MAC_OFFLINE_KICK_THRESHOLD_S:
            try:
                await _kick_relay_daemons_for_post_online_catchup()
            except Exception as e:
                log.warning("[Mac watchdog] post-online kick failed: %s", e)

    elif ok:
        # Still online — background sync runs separately on every scheduler tick (30s)
        _mac_offline_since = 0.0
        _mac_alerted = False

        # --- Auth state tracking + daemon health sweep ---
        global _mac_was_authenticated
        # Periodic auth refresh from Mac (every ~2 min or when cache looks expired)
        cached = get_cached_status()
        should_refresh = (
            not cached.get("_auth_checked")
            or (time.monotonic() - _core._cached_auth_time > _AUTH_REFRESH_INTERVAL_S)
            or not cached.get("authenticated")  # looks expired, re-check Mac
        )
        if should_refresh:
            try:
                await status()  # refreshes _cached_authenticated from Mac
            except Exception as e:
                log.debug("[Mac watchdog] Auth refresh failed: %s", e)

        now_auth = _core._cached_authenticated
        if now_auth and not _mac_was_authenticated:
            # Transition: unauthenticated → authenticated
            log.info("[Mac watchdog] Auth transition: unauthenticated → authenticated")
            _mac_was_authenticated = True
            # Immediate respawn of any dead/missing daemons
            if not _kick_lock.locked():
                asyncio.create_task(_respawn_unhealthy_daemons("auth_transition"))
        elif not now_auth and _mac_was_authenticated:
            _mac_was_authenticated = False
            log.info("[Mac watchdog] Auth transition: authenticated → unauthenticated")
            # Kill all relay daemons — stale SSH pipes could accept queries
            # without re-authentication (guard.sh only validates TOTP at
            # spawn, not per-query on existing connections). FB-48 pen test.
            if not _kick_lock.locked():
                asyncio.create_task(_kill_daemons_on_auth_expiry())

        # Periodic health sweep: respawn any unhealthy daemons (idempotent)
        if now_auth and not _kick_lock.locked():
            await _respawn_unhealthy_daemons("health_sweep")

        # Log orphaned relay_ tmux sessions (advisory only — no raw tmux
        # commands sent via SSH after FB-32 hardening). guard.sh's internal
        # kill_relay_sessions() cleans these up on session expiry/lock.
        try:
            ok_ls, ls_out = await execute("relay-list-tmux", timeout=10.0)
            if ok_ls and ls_out:
                for tmux_name in ls_out.strip().split("\n"):
                    tmux_name = tmux_name.strip()
                    if tmux_name.startswith("relay_"):
                        log.info("[Mac watchdog] Stray tmux session (advisory): %s", tmux_name)
        except Exception as e:
            log.debug("[Mac watchdog] tmux listing failed: %s", e)

        # --- Mac process monitoring (orphan SSH detection) ---
        try:
            snapshot = await get_mac_process_snapshot()
            if snapshot["bot_ssh_stale"]:
                log.warning("[Mac watchdog] STALE SSH procs (>30min): %s",
                            snapshot["bot_ssh_stale"])
            log.info("[Mac watchdog] Process snapshot: ssh_alive=%d, "
                     "ssh_stale=%d, mac_daemons=%d",
                     snapshot["bot_ssh_procs"],
                     len(snapshot["bot_ssh_stale"]),
                     snapshot["mac_daemon_count"])
        except Exception as e:
            log.debug("[Mac watchdog] Process monitoring failed: %s", e)

    elif not ok and _mac_was_online:
        # Transition: online → offline
        _mac_was_online = False
        _mac_offline_since = time.time()
        log.info("[Mac watchdog] Mac went offline")

    elif not ok and not _mac_was_online:
        # Still offline — set offline_since on first check (cold start)
        if _mac_offline_since == 0.0:
            _mac_offline_since = time.time()
        # Alert if >5min AND we've seen Mac online at least once this session.
        # On cold-offline (bot starts with Mac unreachable) we suppress the
        # alert — there was no real online→offline transition, and without
        # this guard every container restart fires a spurious "went offline"
        # to relay chats (_mac_alerted is in-memory and resets on boot).
        if (not _mac_alerted and time.time() - _mac_offline_since > 300):
            _mac_alerted = True
            if _ever_seen_online:
                log.info("[Mac watchdog] Mac offline >5min — alerting relay chats")
                await _alert_relay_chats(
                    "\U0001f4f4 Mac went offline. Messages will not be delivered "
                    "until Mac is back. Resend any failed messages after reconnect."
                )
            else:
                log.info("[Mac watchdog] Cold-offline >5min — suppressing alert "
                         "(Mac never online this session)")


async def _kick_one_chat(
    chat_id: str,
    pool: "RelayClientPool",
    mark_recovered: Callable[[str], None] | None,
) -> tuple[str, bool, str]:
    """Kill+release a single chat's daemon. Used by gather-with-timeout.

    Returns (chat_id, ok, status) — status in {"killed", "no_daemon",
    "in_flight_skipped", "timeout", "error"}.
    """
    from bot.relay.core import _safe_main_attr  # noqa: PLC0415
    from bot.relay.sessions import kill_daemon  # noqa: PLC0415

    # AP-10 fix: never release a pool client that's actively servicing a
    # user query — it would terminate a streaming reply mid-flight. Defer
    # to the next watchdog tick (or to the user's next message after current
    # query completes).
    relay_tasks = _safe_main_attr("_relay_tasks", {})
    active = relay_tasks.get(str(chat_id))
    if active is not None and not active.done():
        log.info("post-online kick chat=%s SKIPPED (in-flight query)", chat_id)
        return (chat_id, False, "in_flight_skipped")

    try:
        mac_kill = await asyncio.wait_for(
            kill_daemon(chat_id), timeout=_PER_CHAT_KICK_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        log.warning("post-online kick chat=%s TIMEOUT after %.0fs",
                    chat_id, _PER_CHAT_KICK_TIMEOUT_S)
        return (chat_id, False, "timeout")
    except Exception as e:
        log.warning("post-online kick chat=%s kill error: %s", chat_id, e)
        return (chat_id, False, "error")

    try:
        await asyncio.wait_for(pool.release(chat_id), timeout=_POOL_RELEASE_TIMEOUT_S)
    except Exception as re_err:
        log.debug("post-online: pool.release failed chat=%s: %s",
                  chat_id, re_err)

    if mark_recovered is not None:
        try:
            mark_recovered(chat_id)
        except Exception as me_err:
            log.debug("post-online: mark_recovered failed chat=%s: %s",
                      chat_id, me_err)

    ok = bool(mac_kill.get("ok"))
    status = "killed" if ok else "no_daemon"
    log.info("post-online kick chat=%s ok=%s killed_pid=%s status=%s",
             chat_id, ok, mac_kill.get("killed_pid"), status)
    return (chat_id, ok, status)


async def _kill_daemons_on_auth_expiry() -> None:
    """Kill all relay daemons when TOTP session expires (FB-48 security fix).

    Stale daemons on live SSH pipes can accept queries without re-auth because
    guard.sh validates TOTP only at spawn time. Kill all daemons + release pool
    clients so no prompt-injected message can reach Mac Claude after auth expires.
    Unlike post-online kick, NO respawn — daemons stay dead until re-auth.
    """
    from bot.relay.registry import _load_registry  # noqa: PLC0415

    async with _kick_lock:
        registry = _load_registry()
        relay_chats = [(cid, e) for cid, e in registry.items()
                       if e.get("relay_mode")]
        if not relay_chats:
            return
        try:
            from bot.relay.pool import get_pool
        except Exception as e:
            log.warning("[Mac watchdog] auth-expiry kill: pool import failed: %s", e)
            return
        pool = get_pool()
        log.warning("[Mac watchdog] Auth expired — killing %d relay daemon(s)",
                    len(relay_chats))
        results = await asyncio.gather(
            *[_kick_one_chat(cid, pool, None) for cid, _ in relay_chats],
            return_exceptions=True,
        )
        status_counts: dict[str, int] = {}
        for r in results:
            if isinstance(r, BaseException):
                status_counts["exception"] = status_counts.get("exception", 0) + 1
                continue
            _cid, _ok, st = r
            status_counts[st] = status_counts.get(st, 0) + 1
        log.warning("[Mac watchdog] Auth-expiry kill complete: %s",
                    ", ".join(f"{k}={v}" for k, v in status_counts.items()))
        # Alert relay chats that auth expired
        try:
            await _alert_relay_chats(
                "\U0001f512 Mac session expired — relay paused. "
                "Use <code>/auth XXXXXX</code> to re-authenticate."
            )
        except Exception:
            pass  # best-effort alert


async def _kick_relay_daemons_for_post_online_catchup() -> None:
    """For each relay_mode chat in the registry (in PARALLEL with per-chat
    timeouts): force-kill its Mac daemon, release the bot-side pool client,
    and tag the chat in main._recently_recovered_chats so the next catchup
    event renders with a "Mac back online" header.

    After kill, for each chat that was SUCCESSFULLY killed (status=="killed"),
    proactively respawn the daemon via pool.acquire() -> ensure_ready().
    Eager respawn fires a fresh catchup within 5-15s of Mac recovery — the
    user sees recent Desktop activity BEFORE needing to send a message.
    Skipped/timed-out chats fall back to lazy-spawn on the user's next msg.

    Eager respawn safety (dual-daemon prevention):
    - Only chats with kill status=="killed" qualify (not "timeout"/"error").
    - Before spawning, list_daemons() verifies the old daemon is gone on
      the Mac — if list lookup fails OR the old PID is still present, we
      SKIP eager respawn and defer to lazy-spawn. Better to miss a 10s
      catchup window than to race two daemons on the same chat.
    - _eager_respawn_one launches detached (asyncio.create_task) so a
      slow spawn doesn't stall the watchdog. Exceptions are logged, not
      raised.

    Called by the watchdog on offline->online transition (only if Mac was
    offline >=60s -- short blips don't need cascading respawns).

    Concurrency safety:
    - Held by `_kick_lock`: prevents overlapping watchdog ticks from
      double-firing kicks while a prior cascade is in flight (AP-7).
    - Per-chat parallelism via `asyncio.gather(return_exceptions=True)`
      with 8s timeout each: a hung SSH on one chat does not stall the
      watchdog or the other chats (AP-13).
    - Skips chats with in-flight queries (AP-10): killing mid-query would
      corrupt the user's streaming reply. Skipped chats catch up via the
      realtime tailer until the user's next message (which triggers
      respawn organically when the prior query exits).

    Failure-safe: all per-chat outcomes captured into status counts; the
    overall function never raises.
    """
    from bot.relay.core import _safe_main_attr  # noqa: PLC0415
    from bot.relay.registry import _load_registry  # noqa: PLC0415

    if _kick_lock.locked():
        log.info("[Mac watchdog] post-online kick already in progress — skip")
        return
    async with _kick_lock:
        registry = _load_registry()
        if not registry:
            return
        relay_chats = [(cid, e) for cid, e in registry.items()
                       if e.get("relay_mode")]
        if not relay_chats:
            return
        try:
            from bot.relay.pool import get_pool
        except Exception as e:
            log.warning("post-online kick: pool import failed: %s", e)
            return
        mark_recovered = _safe_main_attr("mark_relay_recovered")
        pool = get_pool()
        log.info("[Mac watchdog] post-online: candidate relay chats=%d",
                 len(relay_chats))
        results = await asyncio.gather(
            *[_kick_one_chat(cid, pool, mark_recovered)
              for cid, _ in relay_chats],
            return_exceptions=True,
        )
        # Summarise outcomes for observability
        status_counts: dict[str, int] = {}
        killed_chats: list[tuple[str, dict]] = []
        registry_by_cid = dict(relay_chats)
        for r in results:
            if isinstance(r, BaseException):
                status_counts["exception"] = status_counts.get("exception", 0) + 1
                continue
            cid, _ok, st = r
            status_counts[st] = status_counts.get(st, 0) + 1
            if st == "killed" and cid in registry_by_cid:
                killed_chats.append((cid, registry_by_cid[cid]))
        log.info("[Mac watchdog] post-online kick complete: %s",
                 ", ".join(f"{k}={v}" for k, v in status_counts.items()))

        # Eager respawn: proactively bring daemons back online so the
        # realtime tailer can forward last 10 turns before user messages.
        if killed_chats:
            await _eager_respawn_after_kick(killed_chats, pool)


async def _eager_respawn_after_kick(
    killed_chats: list[tuple[str, dict]],
    pool: "RelayClientPool",
) -> None:
    """For each successfully-killed chat, verify the Mac daemon is truly
    gone, then spawn a fresh daemon detached (fire-and-forget).

    Dual-daemon prevention: list_daemons() gives us the current Mac-side
    daemon roster; if our chat_id still appears, we SKIP respawn (kill
    didn't fully take; respawning would race with whatever is left).
    """
    from bot.relay.sessions import list_daemons  # noqa: PLC0415
    from bot.relay.registry import _uuid_from_entry  # noqa: PLC0415

    try:
        alive = await list_daemons()
    except Exception as e:
        log.warning("[Mac watchdog] eager respawn: list_daemons failed, skipping: %s", e)
        return
    if not alive.get("ok"):
        log.warning("[Mac watchdog] eager respawn: list_daemons not ok, skipping: %s",
                    alive.get("error"))
        return
    alive_chats: set[str] = {
        str(d.get("chat_id", "")) for d in alive.get("daemons", [])
    }
    spawned = 0
    skipped_still_alive = 0
    skipped_no_uuid = 0
    # Stagger respawns to prevent simultaneous catchup digests from
    # triggering TG FloodWait. Each daemon fires 10+ TG messages on
    # catchup — spacing them 3s apart keeps the burst under rate limits.
    _RESPAWN_STAGGER_S = 3.0
    for i, (chat_id, entry) in enumerate(killed_chats):
        if chat_id in alive_chats:
            # Kill didn't fully take — don't race a respawn with the old
            # process. Lazy-spawn on next user message will handle it.
            log.warning(
                "[Mac watchdog] eager respawn SKIP chat=%s: old daemon still alive "
                "(kill didn't take)", chat_id
            )
            skipped_still_alive += 1
            continue
        uuid = _uuid_from_entry(entry)
        cwd = entry.get("project_path") or ""
        if not uuid or not cwd:
            log.info(
                "[Mac watchdog] eager respawn SKIP chat=%s: missing uuid/cwd in registry",
                chat_id,
            )
            skipped_no_uuid += 1
            continue
        # Stagger between spawns to avoid concurrent catchup digests
        if spawned > 0:
            await asyncio.sleep(_RESPAWN_STAGGER_S)
        # Detached spawn: fire-and-forget, exceptions logged.
        asyncio.create_task(_eager_respawn_one(chat_id, uuid, cwd, pool))
        spawned += 1
    log.info(
        "[Mac watchdog] eager respawn: spawned=%d skipped_still_alive=%d skipped_no_uuid=%d",
        spawned, skipped_still_alive, skipped_no_uuid,
    )


async def _eager_respawn_one(
    chat_id: str, uuid: str, cwd: str, pool: "RelayClientPool",
) -> None:
    """Spawn one daemon, bounded by 20s total time. Logs and swallows errors."""
    try:
        client = await asyncio.wait_for(
            pool.acquire(chat_id, uuid, cwd), timeout=10.0
        )
        await asyncio.wait_for(client.ensure_ready(), timeout=20.0)
        log.info(
            "[Mac watchdog] eager respawn OK chat=%s uuid=%s", chat_id, uuid[:8],
        )
    except asyncio.TimeoutError:
        log.warning(
            "[Mac watchdog] eager respawn TIMEOUT chat=%s — lazy-spawn will retry",
            chat_id,
        )
    except Exception as e:
        log.warning(
            "[Mac watchdog] eager respawn FAILED chat=%s: %s — lazy-spawn will retry",
            chat_id, e,
        )


async def _respawn_unhealthy_daemons(reason: str = "health_check") -> None:
    """Check all relay chats for unhealthy/missing pool clients and respawn them.

    Same acquire+ensure_ready pattern as startup ``_eager_spawn_relay_daemons``.
    Idempotent: healthy daemons (READY/BUSY/CONNECTING/RETRY) are skipped.
    Per-chat cooldown prevents infinite respawn of persistently failing daemons.

    Called from:
    - ``authenticate()`` on AUTH_OK  -> instant respawn after re-auth
    - watchdog auth transition        -> unauthenticated->authenticated
    - watchdog periodic health sweep  -> every online+authenticated tick
    """
    from bot.relay.core import DESKTOP_ENABLED  # noqa: PLC0415
    from bot.relay.registry import _load_registry, _uuid_from_entry  # noqa: PLC0415

    if not DESKTOP_ENABLED:
        return
    from bot.relay.pool import get_pool, ClientState  # noqa: PLC0415
    pool = get_pool()
    registry = _load_registry()
    now = time.monotonic()

    # Auth transitions bypass cooldown — user explicitly re-authed
    bypass_cooldown = reason in ("post_auth", "auth_transition")

    spawned = 0
    cooldown_skipped = 0
    for cid, entry in registry.items():
        if not entry.get("relay_mode"):
            continue
        uuid = _uuid_from_entry(entry)
        cwd = entry.get("project_path") or ""
        if not uuid or not cwd:
            continue

        # Check existing client health
        existing = pool.get_client(cid)
        if existing and existing.state in (
            ClientState.READY, ClientState.BUSY,
            ClientState.CONNECTING, ClientState.RETRY,
        ):
            # Healthy — clear any cooldown entry
            _respawn_last_attempt.pop(cid, None)
            continue  # healthy or already being handled

        # Per-chat cooldown: don't re-attempt if we tried recently
        if not bypass_cooldown:
            last = _respawn_last_attempt.get(cid, 0.0)
            if now - last < _RESPAWN_COOLDOWN_S:
                cooldown_skipped += 1
                continue

        # Unhealthy (DEAD/ERROR/DISCONNECTED) or missing — fire-and-forget spawn
        _respawn_last_attempt[cid] = now
        asyncio.create_task(_eager_respawn_one(cid, uuid, cwd, pool))
        spawned += 1

    if spawned or cooldown_skipped:
        log.info("[Mac relay] respawn_unhealthy reason=%s spawned=%d cooldown_skipped=%d",
                 reason, spawned, cooldown_skipped)


async def _alert_relay_chats(message: str):
    """Send a notification to all relay_mode chats."""
    from integrations.telegram import send_message
    from bot.relay.registry import _load_registry  # noqa: PLC0415

    registry = _load_registry()
    for chat_id, entry in registry.items():
        if entry.get("relay_mode"):
            try:
                tg_cid = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
                await send_message(message, chat_id=tg_cid, parse_mode="html")
            except Exception as e:
                log.debug(f"Alert send failed for chat {chat_id}: {e}")
