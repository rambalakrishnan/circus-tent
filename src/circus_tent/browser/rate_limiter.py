"""Target-side rate limiting: RPM ceilings, session step ceilings, backoff (B6).

See modules/browser/.omp-spec.md.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from circus_tent.config.loader import BackoffConfig


@dataclass
class BackoffState:
    active: bool
    until: float
    wait_seconds: float

    def next_wait(self) -> float:
        """Double the current wait (caller caps at max_wait_seconds)."""
        return self.wait_seconds * 2.0


class RateLimited(Exception):
    """Raised when the shard is in backoff/cooldown and cannot take requests."""

    def __init__(self, shard: str, until: float) -> None:
        self.shard = shard
        self.until = until
        super().__init__(f"shard {shard!r} rate-limited until {until}")


class _TokenBucket:
    """Token bucket: `rate` tokens/sec, capacity = rate (1-minute burst)."""

    def __init__(self, rate: float, clock: Callable[[], float]) -> None:
        self.rate = float(rate)
        self.capacity = self.rate
        self.tokens = self.rate
        self._clock = clock
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def wait_seconds(self) -> float:
        """Seconds until one token is available (0.0 if available now)."""
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def consume(self) -> None:
        self._refill()
        self.tokens -= 1.0


class TargetRateLimiter:
    """Per-shard RPM + per-session step-rate token buckets; per-shard backoff.

    Backoff engages on 429/403/503 or challenge pages (per-shard, not per-tab)
    so a rate-limit signal pauses ALL traffic to the shard. While backoff is
    active, before_request raises RateLimited and the cluster freezes the
    shard's page counter during the cooldown window.
    """

    def __init__(
        self,
        requests_per_minute: float,
        session_steps_per_minute: float,
        backoff: BackoffConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._shard_bucket = _TokenBucket(requests_per_minute / 60.0, clock)
        self._session_rate = session_steps_per_minute / 60.0
        self._session_buckets: dict[str, _TokenBucket] = {}
        self._backoff_cfg = backoff
        self._backoff = BackoffState(
            active=False, until=0.0, wait_seconds=backoff.initial_wait_seconds
        )
        self._backoff_count = 0

    async def before_request(self, session_key: str | None) -> None:
        """Await permit; raise RateLimited if the shard is backed off."""
        if self._backoff.active:
            if self._clock() < self._backoff.until:
                raise RateLimited("shard", self._backoff.until)
            self._backoff.active = False
        waits = [self._shard_bucket.wait_seconds()]
        if session_key is not None:
            bucket = self._session_buckets.get(session_key)
            if bucket is None:
                bucket = _TokenBucket(self._session_rate, self._clock)
                self._session_buckets[session_key] = bucket
            waits.append(bucket.wait_seconds())
        wait = max(waits)
        if wait > 0:
            await asyncio.sleep(wait)
        self._shard_bucket.consume()
        if session_key is not None:
            self._session_buckets[session_key].consume()

    def on_response(self, status: int, is_challenge_page: bool) -> None:
        """Engage backoff on 429/403/503 or challenge pages."""
        if status not in self._backoff_cfg.triggers and not is_challenge_page:
            return
        self._backoff_count += 1
        self._backoff.wait_seconds = min(
            self._backoff_cfg.initial_wait_seconds * (2 ** (self._backoff_count - 1)),
            self._backoff_cfg.max_wait_seconds,
        )
        self._backoff.active = True
        self._backoff.until = self._clock() + self._backoff.wait_seconds

    def cooldown_remaining(self) -> float:
        """Seconds of backoff remaining; page counter frozen while > 0."""
        if not self._backoff.active:
            return 0.0
        return max(0.0, self._backoff.until - self._clock())

    def snapshot(self) -> dict[str, Any]:
        return {
            "backoff_active": self._backoff.active,
            "backoff_until": self._backoff.until,
            "backoff_count": self._backoff_count,
            "cooldown_remaining": self.cooldown_remaining(),
            "sessions_tracked": len(self._session_buckets),
        }
