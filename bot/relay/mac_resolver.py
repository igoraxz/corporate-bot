# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Dynamic employee Mac IP resolver — multi-tier fallback chain.

Replaces the hardcoded `100.x.y.z` pin in `bot/desktop_relay.py`
and `bot/relay/pool.py` so SSH to employee workstations survives
Tailscale IP changes (device re-auth, tailnet restructure, etc.).

Tier priority (highest → lowest):
  1. DESKTOP_HOST_IP env — explicit pin, bypasses all auto-resolution.
  2. Tailscale API — OAuth client_credentials → devices endpoint → match
     by hostname. Requires TAILSCALE_OAUTH_CLIENT_ID + _SECRET env vars.
  3. DNS — socket.gethostbyname(DESKTOP_HOST) via container's resolver.
  4. Disk cache — last successful result from `data/mac_ip_cache.json`.
  5. Hardcoded fallback — Desktop Mac IP as of 2026-04 (`_DEFAULT_FALLBACK_IP`).

The chain is FAIL-SAFE: any tier failing falls through to the next; the
final hardcoded fallback guarantees the function never raises.

Public API:
  - get_mac_ip() -> str               # sync, always returns a valid IP
  - refresh_mac_ip() -> RefreshResult  # async, runs the chain, updates cache
  - get_ssh_host_opts() -> list[str]  # sync, returns ["-o", f"HostName={ip}"]

Caching:
  In-memory (_current_ip, _last_refresh_t) avoids per-call network work.
  Disk cache (data/mac_ip_cache.json) survives container restarts.
  TTL: MAC_IP_TTL_S env (default 300s) — refresh_mac_ip respects it.

Background refresh is driven by mac_health_watchdog (60s cadence) —
this module never schedules its own refresh loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# Config (env + constants)
# ─────────────────────────────────────────────────────────────────────────

DESKTOP_HOST = os.environ.get("DESKTOP_HOST", "your-mac")  # Set via DESKTOP_HOST env var
_DEFAULT_FALLBACK_IP = ""  # Empty = fail-closed when no resolution succeeds
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_CACHE_FILE = _DATA_DIR / "config" / "mac_ip_cache.json"
_TTL_S: float = float(os.environ.get("MAC_IP_TTL_S", "300"))
_TAILSCALE_API_BASE = os.environ.get(
    "TAILSCALE_API_BASE", "https://api.tailscale.com"
)
_TAILSCALE_TAILNET = os.environ.get("TAILSCALE_TAILNET", "-")

# Tier 2 (Tailscale API) credentials — optional; if unset, tier is skipped.
# Strip trailing whitespace — a .env file with a trailing newline on the
# quoted value would otherwise break the OAuth POST with a silent 401.
_TAILSCALE_CLIENT_ID = os.environ.get("TAILSCALE_OAUTH_CLIENT_ID", "").strip()
from config import TAILSCALE_OAUTH_CLIENT_SECRET as _TS_SECRET
_TAILSCALE_CLIENT_SECRET = _TS_SECRET.strip() if _TS_SECRET else ""

# Valid IPv4 regex check (cheap sanity; full validation via socket.inet_aton)
def _is_valid_ipv4(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────────────────

_current_ip: str = _DEFAULT_FALLBACK_IP
_current_provider: str = "default"  # which tier produced _current_ip
_last_refresh_t: float = 0.0
_refresh_lock: asyncio.Lock = asyncio.Lock()


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of one refresh_mac_ip() call."""
    ip: str
    provider: str  # env | tailscale | dns | cache | default
    changed: bool  # True if _current_ip was updated vs prior value
    elapsed_s: float


# ─────────────────────────────────────────────────────────────────────────
# Cache I/O (disk)
# ─────────────────────────────────────────────────────────────────────────

def _load_cache() -> tuple[str, str] | None:
    """Return (ip, provider) from disk cache, or None if unavailable/invalid."""
    try:
        if not _CACHE_FILE.exists():
            return None
        data = json.loads(_CACHE_FILE.read_text())
        ip = data.get("ip", "")
        provider = data.get("provider", "cache")
        if _is_valid_ipv4(ip):
            return (ip, provider)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug("mac_resolver: cache load failed: %s", e)
    return None


def _save_cache(ip: str, provider: str) -> None:
    """Persist resolved IP + provider + timestamp to disk cache."""
    if not _is_valid_ipv4(ip):
        return
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "ip": ip,
            "provider": provider,
            "updated_at": time.time(),
        }))
    except OSError as e:
        log.debug("mac_resolver: cache save failed: %s", e)


# On import, hydrate from disk if available — avoids a cold-start hardcoded
# fallback window if the container restarts before the first refresh tick.
def _hydrate_from_cache() -> None:
    global _current_ip, _current_provider
    cached = _load_cache()
    if cached:
        ip, prov = cached
        _current_ip = ip
        _current_provider = f"cache(from={prov})"
        log.info("mac_resolver: hydrated from disk cache ip=%s prior_provider=%s",
                 ip, prov)


_hydrate_from_cache()


# ─────────────────────────────────────────────────────────────────────────
# Tier 1: env pin (explicit override)
# ─────────────────────────────────────────────────────────────────────────

def _resolve_env_pin() -> str | None:
    """Return DESKTOP_HOST_IP if set + valid, else None. Re-read each call
    so a container restart picks up docker-compose changes immediately."""
    pin = os.environ.get("DESKTOP_HOST_IP", "").strip()
    if pin and _is_valid_ipv4(pin):
        return pin
    if pin:
        log.warning("mac_resolver: DESKTOP_HOST_IP=%r is not a valid IPv4 — ignoring", pin)
    return None


# ─────────────────────────────────────────────────────────────────────────
# Tier 2: Tailscale API
# ─────────────────────────────────────────────────────────────────────────

_TAILSCALE_TIMEOUT_S = 10.0


def _blocking_fetch_tailscale_token(client_id: str, client_secret: str) -> str:
    """OAuth client_credentials token exchange (sync, for executor)."""
    url = f"{_TAILSCALE_API_BASE}/api/v2/oauth/token"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=_TAILSCALE_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token", "")
    if not token:
        raise ValueError("tailscale: empty access_token in response")
    return token


def _blocking_fetch_tailscale_devices(token: str, tailnet: str) -> list[dict]:
    """GET /api/v2/tailnet/{tailnet}/devices (sync, for executor)."""
    url = f"{_TAILSCALE_API_BASE}/api/v2/tailnet/{urllib.parse.quote(tailnet)}/devices"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=_TAILSCALE_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    devices = payload.get("devices", [])
    if not isinstance(devices, list):
        raise ValueError("tailscale: devices field is not a list")
    return devices


def _match_device_ip(devices: list[dict], hostname: str) -> str | None:
    """Find device by hostname (case-insensitive) and return its first IPv4."""
    want = hostname.lower().strip()
    for d in devices:
        if not isinstance(d, dict):
            continue
        # Tailscale returns hostname in multiple fields; check common ones.
        candidates = [
            str(d.get("hostname", "")),
            str(d.get("name", "")).split(".", 1)[0],  # strip tailnet suffix
        ]
        if any(c.lower().strip() == want for c in candidates if c):
            addrs = d.get("addresses", []) or []
            for a in addrs:
                if _is_valid_ipv4(str(a)):
                    return str(a)
    return None


async def _resolve_tailscale() -> str | None:
    """Tier 2: OAuth client_credentials → devices → hostname match.

    Runs blocking urllib calls in a thread executor. Returns None on any
    failure (missing creds, API error, hostname not found).
    """
    if not _TAILSCALE_CLIENT_ID or not _TAILSCALE_CLIENT_SECRET:
        return None
    try:
        loop = asyncio.get_running_loop()
        token = await loop.run_in_executor(
            None, _blocking_fetch_tailscale_token,
            _TAILSCALE_CLIENT_ID, _TAILSCALE_CLIENT_SECRET,
        )
        devices = await loop.run_in_executor(
            None, _blocking_fetch_tailscale_devices, token, _TAILSCALE_TAILNET,
        )
        ip = _match_device_ip(devices, DESKTOP_HOST)
        if ip:
            return ip
        log.warning("mac_resolver: Tailscale devices returned %d entries but none "
                    "matched hostname=%r", len(devices), DESKTOP_HOST)
        return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, OSError) as e:
        log.warning("mac_resolver: Tailscale tier failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────
# Tier 3: DNS
# ─────────────────────────────────────────────────────────────────────────

def _is_tailnet_ipv4(ip: str) -> bool:
    """True if ip is in Tailscale's CGNAT range 100.64.0.0/10.

    Used to harden DNS tier against poisoned resolvers: a rogue DNS
    response could return a public IP (e.g. 8.8.8.8), and SSH with
    `StrictHostKeyChecking=accept-new` would dutifully auto-pin the
    host key. Constrain DNS-returned IPs to Tailscale CGNAT (100.64/10)
    so a spoof leads only to a connection failure, not a key pin.
    """
    if not _is_valid_ipv4(ip):
        return False
    try:
        first, second = ip.split(".", 2)[:2]
        if int(first) != 100:
            return False
        # CGNAT is 100.64.0.0/10 = 100.64-127.x.x
        return 64 <= int(second) <= 127
    except (ValueError, IndexError):
        return False


async def _resolve_dns() -> str | None:
    """Tier 3: socket.gethostbyname in executor (blocking call).

    Constrains the result to the Tailscale CGNAT range (100.64.0.0/10)
    via _is_tailnet_ipv4 — a poisoned resolver returning a public IP
    would be rejected here so SSH never auto-pins a bogus host key.
    """
    try:
        loop = asyncio.get_running_loop()
        ip = await loop.run_in_executor(None, socket.gethostbyname, DESKTOP_HOST)
        if not _is_valid_ipv4(ip):
            return None
        if not _is_tailnet_ipv4(ip):
            log.warning(
                "mac_resolver: DNS returned non-tailnet IP=%s for %s — rejecting "
                "(possible DNS poisoning; SSH would blindly pin host key)",
                ip, DESKTOP_HOST,
            )
            return None
        return ip
    except (socket.gaierror, OSError) as e:
        log.debug("mac_resolver: DNS tier failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────

def get_mac_ip() -> str:
    """Return the currently-cached Mac IP. Sync, always returns a valid IP.

    Never blocks on network — refresh_mac_ip() is the only async path.
    If the cache is cold (never refreshed), returns the hardcoded fallback
    or disk-hydrated value (see _hydrate_from_cache at module load).
    """
    return _current_ip


def get_current_provider() -> str:
    """Return the name of the tier that produced the current IP (for logging)."""
    return _current_provider


def get_ssh_host_opts(hostname_override: str = "") -> list[str]:
    """Return SSH -o HostName=<ip> option pair, using the current cached IP.

    CB-140: If hostname_override is provided (per-user workstation), use it
    instead of the global resolved IP. The override is a hostname, not an IP —
    SSH will resolve it.
    """
    if hostname_override and hostname_override != DESKTOP_HOST:
        # Per-user host — let SSH resolve the hostname directly
        return ["-o", f"HostName={hostname_override}"]
    return ["-o", f"HostName={_current_ip}"]


async def refresh_mac_ip(force: bool = False) -> RefreshResult:
    """Run the resolution chain. Updates in-memory + disk cache on success.

    Honors MAC_IP_TTL_S: returns cached result without network work if the
    last successful refresh was within TTL. Pass force=True to bypass TTL.

    Thread-safe: protected by _refresh_lock so concurrent callers (watchdog
    tick + on-demand refresh) don't race.
    """
    global _current_ip, _current_provider, _last_refresh_t

    async with _refresh_lock:
        now = time.time()
        if not force and (now - _last_refresh_t) < _TTL_S:
            return RefreshResult(
                ip=_current_ip, provider=_current_provider,
                changed=False, elapsed_s=0.0,
            )

        start = now
        prior_ip = _current_ip

        # Tier 1: env pin (highest priority).
        ip = _resolve_env_pin()
        if ip:
            return _commit(ip, "env", prior_ip, start)

        # Tier 2: Tailscale API.
        ip = await _resolve_tailscale()
        if ip:
            return _commit(ip, "tailscale", prior_ip, start)

        # Tier 3: DNS.
        ip = await _resolve_dns()
        if ip:
            return _commit(ip, "dns", prior_ip, start)

        # Tier 4: disk cache (re-read in case another process updated it).
        # Only useful as a COLD-START fallback — if we already have a
        # non-default current_ip (hydrated at import or set by prior
        # refresh), the cache has the same value and reading it adds
        # nothing. Skip so _last_refresh_t is NOT bumped and the next
        # tick retries tiers 1-3.
        if prior_ip == _DEFAULT_FALLBACK_IP:
            cached = _load_cache()
            if cached and cached[0] != _DEFAULT_FALLBACK_IP:
                return _commit(cached[0], f"cache(prior={cached[1]})",
                               prior_ip, start, bump_ttl=True)

        # Tier 5: hardcoded fallback (only reached on cold start with no
        # disk cache). Preserve whatever _current_ip was — if it's already
        # non-default (set by a previous successful refresh), keep it and
        # do NOT bump _last_refresh_t so the next tick retries live tiers.
        if prior_ip != _DEFAULT_FALLBACK_IP:
            return _commit(prior_ip, f"stale({_current_provider})",
                           prior_ip, start, persist=False, bump_ttl=False)
        return _commit(_DEFAULT_FALLBACK_IP, "default", prior_ip, start,
                       persist=False, bump_ttl=False)


def _commit(
    ip: str, provider: str, prior_ip: str, start_t: float,
    persist: bool = True, bump_ttl: bool = True,
) -> RefreshResult:
    """Apply a resolved IP to in-memory state + optionally disk cache.

    `bump_ttl` controls whether `_last_refresh_t` is updated. Live tiers
    (env/tailscale/dns) bump it to suppress near-term retries within TTL.
    Stale/default tiers leave it alone so the next tick retries immediately.
    """
    global _current_ip, _current_provider, _last_refresh_t
    elapsed = time.time() - start_t
    changed = (ip != prior_ip)
    _current_ip = ip
    _current_provider = provider
    if bump_ttl:
        _last_refresh_t = time.time()
    if persist and provider not in ("default",) and not provider.startswith("stale"):
        _save_cache(ip, provider)
    if changed:
        log.info(
            "mac_resolver: IP updated provider=%s old=%s new=%s elapsed=%.2fs",
            provider, prior_ip, ip, elapsed,
        )
    else:
        log.debug(
            "mac_resolver: IP unchanged provider=%s ip=%s elapsed=%.2fs",
            provider, ip, elapsed,
        )
    return RefreshResult(ip=ip, provider=provider, changed=changed, elapsed_s=elapsed)
