"""Fair-share permit distribution (acceptance #4). See spec.

FIFO by arrival: permits are granted to the oldest waiting key that is under
its quota. A key at its quota waits for its own releases; keys behind it can
still be served (no head-of-line blocking). This guarantees no key starves
another and no key ever exceeds its quota.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class QueueTooDeep(Exception):
    def __init__(self, depth: int, limit: int) -> None:
        self.depth = depth
        self.limit = limit
        super().__init__(f"queue depth {depth} exceeds limit {limit}")


class _Waiter:
    __slots__ = ("key", "fut", "quota", "enqueued_at")

    def __init__(self, key: str, fut: asyncio.Future[None], quota: int, enqueued_at: float) -> None:
        self.key = key
        self.fut = fut
        self.quota = quota
        self.enqueued_at = enqueued_at


class FairShare:
    """FIFO permit pool with per-key quotas. No key may starve another."""

    def __init__(
        self,
        max_permits: int,
        default_quota: int = 3,
        max_queue_depth: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_permits = max_permits
        self._default_quota = default_quota
        self._max_queue_depth = max_queue_depth
        self._clock = clock
        self._available = max_permits
        self._held: dict[str, int] = {}
        self._queue: deque[_Waiter] = deque()

    async def acquire(self, key: str, quota: int | None = None) -> None:
        limit = quota if quota is not None else self._default_quota
        if limit <= 0:
            raise ValueError("quota must be positive")
        if self._available > 0 and self._held.get(key, 0) < limit and not self._queue:
            self._grant(key)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if len(self._queue) >= self._max_queue_depth:
            raise QueueTooDeep(len(self._queue), self._max_queue_depth)
        self._queue.append(_Waiter(key, fut, limit, self._clock()))
        await fut

    def _grant(self, key: str) -> None:
        self._available -= 1
        self._held[key] = self._held.get(key, 0) + 1

    def _wake(self) -> None:
        while self._queue and self._available > 0:
            for waiter in self._queue:
                if self._held.get(waiter.key, 0) < waiter.quota:
                    self._queue.remove(waiter)
                    self._grant(waiter.key)
                    if not waiter.fut.done():
                        waiter.fut.set_result(None)
                    break
            else:
                return

    def release(self, key: str) -> None:
        held = self._held.get(key, 0)
        if held <= 0:
            return
        self._held[key] = held - 1
        self._available += 1
        self._wake()

    def queue_depth(self) -> int:
        return len(self._queue)

    def wait_time_ms(self, key: str) -> float:
        now = self._clock()
        oldest = None
        for waiter in self._queue:
            if waiter.key == key:
                oldest = waiter.enqueued_at
                break
        if oldest is None:
            return 0.0
        return max(0.0, (now - oldest) * 1000.0)

    def snapshot(self) -> dict[str, Any]:
        waits = {k: self.wait_time_ms(k) for k in {w.key for w in self._queue}}
        return {
            "holds": dict(self._held),
            "waiting": len(self._queue),
            "queue_depth": len(self._queue),
            "wait_ms": waits,
        }
