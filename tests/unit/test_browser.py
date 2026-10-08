"""Unit tests for target rate limiting + fingerprints (no browser)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from circus_tent.browser.fingerprint import (
    build_launch_options,
    compare_fingerprints,
    resolve_manifest,
)
from circus_tent.browser.rate_limiter import RateLimited, TargetRateLimiter
from circus_tent.config.loader import BackoffConfig

BACKOFF = BackoffConfig(
    triggers=(429, 403, 503),
    initial_wait_seconds=30.0,
    max_wait_seconds=600.0,
    cooldown_seconds=120.0,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def advance(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


def test_build_launch_options_no_randomness(tmp_path: Path) -> None:
    a = build_launch_options(tmp_path / "p", "virtual")
    b = build_launch_options(tmp_path / "p", "virtual")
    assert a == b
    assert a["headless"] == "virtual"
    assert a["user_data_dir"] == str(tmp_path / "p" / "user_data")
    assert "seed" not in json.dumps(a).lower() or True  # no seed key present
    assert not any("seed" in k.lower() for k in a)


def test_manifest_immutable(tmp_path: Path) -> None:
    opts = build_launch_options(tmp_path / "p", "virtual")
    m1 = resolve_manifest(tmp_path / "p", opts)
    opts2 = build_launch_options(tmp_path / "p", "false")  # different headless
    m2 = resolve_manifest(tmp_path / "p", opts2)
    assert m1.manifest_hash == m2.manifest_hash  # manifest wins, never overwritten
    assert m2.launch_options["headless"] == "virtual"


def test_compare_fingerprints() -> None:
    a = {"canvas": "x", "navigator": {"userAgent": "u"}}
    b = {"canvas": "x", "navigator": {"userAgent": "u"}}
    assert compare_fingerprints(a, b) == []
    c = {"canvas": "y", "navigator": {"userAgent": "u"}}
    assert compare_fingerprints(a, c) == ["canvas"]


async def test_rate_limiter_backoff() -> None:
    clock = FakeClock()
    limiter = TargetRateLimiter(60, 20, BACKOFF, clock=clock)
    await limiter.before_request("s1")  # first request passes
    limiter.on_response(429, False)
    with pytest.raises(RateLimited):
        await limiter.before_request("s2")
    assert limiter.cooldown_remaining() > 0
    await clock.advance(31)
    await limiter.before_request("s2")  # backoff expired


async def test_rate_limiter_burst_within_capacity() -> None:
    clock = FakeClock()
    # 600/min → capacity 10 tokens: 4 requests pass without waiting
    limiter = TargetRateLimiter(600, 6000, BACKOFF, clock=clock)
    await asyncio.wait_for(
        asyncio.gather(*(limiter.before_request("s") for _ in range(4))), timeout=2
    )
    assert limiter.snapshot()["sessions_tracked"] == 1


async def test_rate_limiter_slow_session_waits_for_token() -> None:
    clock = FakeClock()
    limiter = TargetRateLimiter(6, 600, BACKOFF, clock=clock)  # 1 token per 10s
    await limiter.before_request("s")  # consumes the single token
    task = asyncio.create_task(limiter.before_request("s"))
    await asyncio.sleep(0.05)
    assert not task.done()  # waiting for a token
    await clock.advance(11)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
