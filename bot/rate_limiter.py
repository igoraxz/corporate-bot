# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Corporate Bot Platform
"""Simple in-memory token bucket rate limiter for webhook endpoints.

Lightweight — no Redis or external dependency. Designed for per-IP
rate limiting on Teams and GitHub webhook routes.

Usage:
    from bot.rate_limiter import webhook_limiter

    @app.post("/api/teams-messages")
    async def teams_webhook(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if not webhook_limiter.allow(client_ip):
            return Response(status_code=429, content="Too Many Requests")
        ...
"""

import time
from collections import defaultdict

import logging

log = logging.getLogger(__name__)


class RateLimiter:
    """Simple in-memory sliding window rate limiter.

    Tracks request timestamps per key (typically IP address).
    Thread-safe for single-process async usage (GIL protects list ops).
    """

    def __init__(self, requests_per_minute: int = 60, burst: int = 10):
        """Initialize rate limiter.

        Args:
            requests_per_minute: Max sustained requests per key per minute.
            burst: Not used separately — rpm is the hard cap.
                   Kept for API compatibility.
        """
        self.rpm = requests_per_minute
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup: float = 0.0

    def allow(self, key: str) -> bool:
        """Check if a request from this key is allowed.

        Args:
            key: Rate limit key (typically client IP or user ID).

        Returns: True if allowed, False if rate limited.
        """
        now = time.monotonic()
        window = 60.0  # 1-minute sliding window

        bucket = self._buckets[key]

        # Purge timestamps older than the window
        bucket[:] = [t for t in bucket if now - t < window]

        if len(bucket) >= self.rpm:
            return False

        bucket.append(now)

        # Periodic cleanup of stale keys (every 5 minutes)
        if now - self._last_cleanup > 300:
            self._cleanup(now, window)

        return True

    def _cleanup(self, now: float, window: float) -> None:
        """Remove empty buckets to prevent memory growth."""
        self._last_cleanup = now
        stale_keys = [
            k for k, v in self._buckets.items()
            if not v or (now - v[-1]) > window
        ]
        for k in stale_keys:
            del self._buckets[k]
        if stale_keys:
            log.debug("RateLimiter: cleaned %d stale keys", len(stale_keys))


# Singleton instances for webhook routes
webhook_limiter = RateLimiter(requests_per_minute=120)
github_webhook_limiter = RateLimiter(requests_per_minute=30)
