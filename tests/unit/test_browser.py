"""Unit tests for target rate limiting + fingerprints (no browser)."""

from __future__ import annotations

import asyncio
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


def test_build_launch_options_pins_fingerprint(tmp_path: Path) -> None:
    fp: dict = {"navigator": {"userAgent": "fixed"}, "webgl": {"renderer": "fixed"}}
    a = build_launch_options(tmp_path / "p", "virtual", fingerprint=fp)
    b = build_launch_options(tmp_path / "p", "virtual", fingerprint=fp)
    assert a == b
    assert a["headless"] == "virtual"
    assert a["user_data_dir"] == str(tmp_path / "p" / "user_data")
    assert a["persistent_context"] is True
    assert a["i_know_what_im_doing"] is True
    assert a["fingerprint"] == fp
    assert a["os"] == "windows"
    assert not any("seed" in k.lower() for k in a)


def test_build_launch_options_generates_fingerprint_when_absent(tmp_path: Path) -> None:
    a = build_launch_options(tmp_path / "p", "virtual")
    assert isinstance(a["fingerprint"], dict)
    assert a["fingerprint"]  # non-empty generated bundle


def test_manifest_immutable_and_pins_fingerprint(tmp_path: Path) -> None:
    fp = {"navigator": {"userAgent": "pinned-ua"}, "webgl": {"renderer": "pinned-gl"}}
    opts = build_launch_options(tmp_path / "p", "virtual", fingerprint=fp)
    m1 = resolve_manifest(tmp_path / "p", opts)
    # a second resolve attempts to change the fingerprint — the manifest wins
    other = build_launch_options(
        tmp_path / "p", "false", fingerprint={"navigator": {"userAgent": "DIFFERENT"}}
    )
    m2 = resolve_manifest(tmp_path / "p", other)
    assert m1.manifest_hash == m2.manifest_hash
    assert m2.launch_options["headless"] == "virtual"
    assert m2.launch_options["fingerprint"] == fp


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
