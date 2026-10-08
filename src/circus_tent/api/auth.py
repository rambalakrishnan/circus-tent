"""Bearer auth + per-caller scoping + per-caller rate limit (B1). See spec."""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable

from circus_tent.config.loader import CallerConfig
from circus_tent.telemetry import Metrics


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message or code)


class _TokenBucket:
    def __init__(self, rate_per_minute: float, clock: Callable[[], float]) -> None:
        self.rate = rate_per_minute / 60.0
        self.tokens = self.rate
        self.capacity = self.rate
        self._clock = clock
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def try_consume(self) -> bool:
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after(self) -> float:
        if self.rate <= 0:
            return 3600.0  # zero-rate bucket: effectively blocked for an hour
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate


class CallerAuth:
    def __init__(
        self,
        callers: tuple[CallerConfig, ...],
        resolve: Callable[[str], str],
        metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._callers = callers
        self._resolve = resolve
        self._metrics = metrics
        self._clock = clock
        self._buckets = {c.id: _TokenBucket(c.rate_limit_per_minute, clock) for c in callers}

    def authenticate(self, authorization: str | None) -> CallerConfig:
        """Bearer parse + constant-time compare against every caller's key."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "UNAUTHORIZED", "missing bearer token")
        token = authorization[7:].strip()
        if not token:
            raise AuthError(401, "UNAUTHORIZED", "empty bearer token")
        for caller in self._callers:
            try:
                expected = self._resolve(caller.key_env)
            except Exception:  # noqa: BLE001
                continue
            if expected and hmac.compare_digest(token, expected):
                return caller
        raise AuthError(401, "UNAUTHORIZED", "invalid bearer token")

    def check_scope(
        self, caller: CallerConfig, tool: str, domain: str, account: str | None
    ) -> None:
        """`domain` here is a shard NAME (service resolves domain→shard first)."""
        if "*" not in caller.tools and tool not in caller.tools:
            raise AuthError(403, "FORBIDDEN", f"caller {caller.id!r} may not invoke tool {tool!r}")
        if "*" not in caller.shards and domain not in caller.shards:
            raise AuthError(
                403, "FORBIDDEN", f"caller {caller.id!r} may not target shard {domain!r}"
            )
        if account is not None and "*" not in caller.accounts and account not in caller.accounts:
            raise AuthError(
                403, "FORBIDDEN", f"caller {caller.id!r} may not act as account {account!r}"
            )

    async def check_rate(self, caller: CallerConfig) -> None:
        bucket = self._buckets[caller.id]
        if bucket.try_consume():
            return
        raise AuthError(
            429,
            "RATE_LIMITED",
            f"caller {caller.id!r} rate limit exceeded; retry after {bucket.retry_after():.0f}s",
        )

    def retry_after(self, caller: CallerConfig) -> float:
        return self._buckets[caller.id].retry_after()
