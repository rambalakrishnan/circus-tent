"""Cluster manager: shard registry, routing, recycling, shutdown. See spec."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from circus_tent.browser.rate_limiter import TargetRateLimiter
from circus_tent.browser.shard import Shard, ShardHealth, ShardState, TabContext
from circus_tent.config.loader import ConfigLoader
from circus_tent.telemetry import Metrics

T = TypeVar("T")


class ShardError(Exception):
    """Shard lifecycle failure (init, health, recycle)."""


@dataclass
class ShardSnapshot:
    health: ShardHealth
    config_name: str
    patterns: tuple[str, ...]


class ClusterManager:
    """Owns all shards; lazy init; routes domains; orchestrates recycling."""

    def __init__(self, loader: ConfigLoader, metrics: Metrics, logger: logging.Logger) -> None:
        self.loader = loader
        self.metrics = metrics
        self.logger = logger
        self._shards: dict[str, Shard] = {}
        self._limiters: dict[str, TargetRateLimiter] = {}
        self._configs = loader.load_shards()
        self._recycle_locks: dict[str, asyncio.Lock] = {}
        self._started = False

    async def start(self, warmup: bool = False) -> None:
        if self._started:
            return
        for cfg in self._configs:
            profile_dir = (self.loader.config_dir.parent / cfg.profile_dir).resolve()
            shard = Shard(cfg, profile_dir, self.metrics, self.logger)
            self._shards[cfg.name] = shard
            self._limiters[cfg.name] = TargetRateLimiter(
                requests_per_minute=cfg.request_rate_per_minute,
                session_steps_per_minute=cfg.session_step_rate_per_minute,
                backoff=cfg.backoff,
            )
            self._recycle_locks[cfg.name] = asyncio.Lock()
        self._started = True
        if warmup:
            for shard in self._shards.values():
                try:
                    await shard.initialize()
                except Exception as e:  # noqa: BLE001
                    self.logger.error(
                        "warmup init failed",
                        extra={"event": "shard_init_failed", "shard": shard.name, "error": str(e)},
                    )

    def shard_for_domain(self, domain: str) -> Shard:
        cfg = self.loader.shard_for_domain(domain, self._configs)
        shard = self._shards.get(cfg.name)
        if shard is None:
            raise ShardError(f"no shard registered for domain {domain!r}")
        return shard

    async def _ensure_ready(self, shard: Shard) -> None:
        if shard.state == ShardState.READY:
            return
        await shard.initialize()

    async def run_tab(
        self,
        domain: str,
        session_key: str | None,
        task: Callable[[TabContext], Awaitable[T]],
    ) -> T:
        shard = self.shard_for_domain(domain)
        limiter = self._limiters[shard.name]
        await limiter.before_request(session_key)
        shard = self.shard_for_domain(domain)  # re-fetch: recycle may have swapped
        await self._ensure_ready(shard)
        tc = await shard.acquire(session_key)

        async def _task_wrapper() -> T:
            try:
                return await task(tc)
            finally:
                await shard.release(tc)
                if limiter.cooldown_remaining() <= 0.0:
                    shard.increment_pages()  # B6: counter frozen during cooldown

        tracked = asyncio.create_task(_task_wrapper())
        shard.track(tracked)
        try:
            return await tracked
        except Exception:
            raise

    def report_response(self, domain: str, status: int, is_challenge: bool) -> None:
        try:
            shard = self.shard_for_domain(domain)
            self._limiters[shard.name].on_response(status, is_challenge)
        except ShardError:
            pass

    async def recycle(self, shard_name: str) -> Shard:
        """Drain → flush → wait PID exit → relaunch SAME profile + SAME manifest
        → health check → atomic registry swap. Never rotates fingerprints."""
        lock = self._recycle_locks.get(shard_name)
        if lock is None:
            raise ShardError(f"unknown shard {shard_name!r}")
        async with lock:
            old = self._shards[shard_name]
            old_pid = old._pid()
            pages = old.snapshot().pages_processed
            started = time.monotonic()
            await old.drain(old.config.recycle_grace_seconds)
            await old.flush()
            await old.terminate()
            replacement = Shard(old.config, old.profile_dir, self.metrics, self.logger)
            last_error: Exception | None = None
            for _ in range(2):
                try:
                    await replacement.initialize()
                    if await replacement.health_check():
                        break
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    await asyncio.sleep(2.0)
            else:
                raise ShardError(
                    f"replacement shard {shard_name!r} failed health check: {last_error}"
                )
            self._shards[shard_name] = replacement
            self.emit_event(
                "shard_recycled",
                shard=shard_name,
                old_pid=old_pid,
                new_pid=replacement._pid(),
                pages=pages,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return replacement

    def snapshots(self) -> list[ShardSnapshot]:
        out = []
        for cfg in self._configs:
            shard = self._shards.get(cfg.name)
            health = (
                shard.snapshot()
                if shard
                else ShardHealth(
                    name=cfg.name,
                    state=ShardState.TERMINATED,
                    tabs_active=0,
                    pages_processed=0,
                    fingerprint_ok=True,
                    pid=None,
                    cooldown_until=None,
                    last_error=None,
                )
            )
            if shard is not None:
                limiter = self._limiters[cfg.name]
                if limiter.cooldown_remaining() > 0:
                    health.cooldown_until = limiter.snapshot()["backoff_until"]
            out.append(
                ShardSnapshot(health=health, config_name=cfg.name, patterns=cfg.domain_patterns)
            )
        return out

    async def shutdown(self, drain_seconds: float = 30.0) -> int:
        """0 = clean drain, 1 = forced. Persists health.json per shard."""
        forced = False
        for shard in self._shards.values():
            try:
                await asyncio.wait_for(shard.drain(drain_seconds), timeout=drain_seconds + 5)
            except TimeoutError:
                forced = True
            await shard.terminate()
            with contextlib.suppress(Exception):
                shard.persist_health()
        return 1 if forced else 0

    def emit_event(self, event: str, **data: Any) -> None:
        self.logger.info(event, extra={"event": event, **data})
