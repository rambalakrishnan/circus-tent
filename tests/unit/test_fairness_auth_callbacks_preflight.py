"""Unit tests for fairness, auth, callbacks, preflight."""

from __future__ import annotations

import asyncio
import logging

import pytest
import respx

from circus_tent.api.auth import AuthError, CallerAuth
from circus_tent.api.callbacks import CallbackClient
from circus_tent.api.fairness import FairShare, QueueTooDeep
from circus_tent.config.loader import CallerConfig
from circus_tent.engine.preflight import check_session
from circus_tent.telemetry import get_metrics

# ---------------------------------------------------------------- fairness


async def test_fairshare_quota_enforced() -> None:
    fs = FairShare(max_permits=12, default_quota=2)
    await fs.acquire("a")
    await fs.acquire("a")
    # third acquire for key a must wait (quota 2)
    task = asyncio.create_task(fs.acquire("a"))
    await asyncio.sleep(0)
    assert not task.done()
    assert fs.snapshot()["holds"] == {"a": 2}
    fs.release("a")
    await asyncio.sleep(0)
    assert task.done()
    fs.release("a")
    fs.release("a")


async def test_fairshare_no_starvation() -> None:
    fs = FairShare(max_permits=2, default_quota=1)
    done: list[str] = []

    async def worker(key: str) -> None:
        for _ in range(5):
            await fs.acquire(key)
            done.append(key)
            await asyncio.sleep(0.01)
            fs.release(key)

    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert done  # all finished — nobody starved
    assert fs.queue_depth() == 0


async def test_fairshare_queue_too_deep() -> None:
    fs = FairShare(max_permits=1, default_quota=1, max_queue_depth=2)
    await fs.acquire("a")
    t1 = asyncio.create_task(fs.acquire("b"))
    t2 = asyncio.create_task(fs.acquire("c"))
    await asyncio.sleep(0)
    with pytest.raises(QueueTooDeep):
        await fs.acquire("d")
    fs.release("a")  # frees the permit → oldest waiter (b) granted
    await asyncio.wait_for(t1, timeout=2)
    fs.release("b")  # frees it again → c granted
    await asyncio.wait_for(t2, timeout=2)


# ---------------------------------------------------------------- auth


def _auth() -> CallerAuth:
    caller = CallerConfig("local-dev", "API_KEY_ENV", ("*",), ("*",), ("*",), 120.0)
    return CallerAuth(
        (caller,), lambda name: "secret-key-123" if name == "API_KEY_ENV" else "", get_metrics()
    )


def test_authenticate_ok_and_missing() -> None:
    auth = _auth()
    caller = auth.authenticate("Bearer secret-key-123")
    assert caller.id == "local-dev"
    with pytest.raises(AuthError) as e:
        auth.authenticate("Bearer wrong")
    assert e.value.status == 401
    with pytest.raises(AuthError):
        auth.authenticate(None)


def test_scope_checks() -> None:
    auth = _auth()
    caller = auth.authenticate("Bearer secret-key-123")
    auth.check_scope(caller, "run", "workday", "neogov")  # wildcards: ok


def test_scope_denied_for_limited_caller() -> None:
    limited = CallerConfig("narrow", "K2", ("workday",), ("run",), ("neogov",), 10.0)
    auth = CallerAuth((limited,), lambda name: "k2-secret", get_metrics())
    caller = auth.authenticate("Bearer k2-secret")
    with pytest.raises(AuthError) as e:
        auth.check_scope(caller, "extract", "workday", "neogov")
    assert e.value.status == 403
    with pytest.raises(AuthError):
        auth.check_scope(caller, "run", "lever", "neogov")


async def test_caller_rate_limit() -> None:
    limited = CallerConfig("slow", "K3", ("*",), ("*",), ("*",), 0.0)
    auth_slow = CallerAuth((limited,), lambda name: "k3-secret", get_metrics())
    slow = auth_slow.authenticate("Bearer k3-secret")
    with pytest.raises(AuthError) as e:
        await auth_slow.check_rate(slow)
    assert e.value.status == 429
    assert auth_slow.retry_after(slow) > 0


# ---------------------------------------------------------------- callbacks


@pytest.mark.asyncio
async def test_callback_notify_signature_and_success() -> None:
    with respx.mock() as mock:
        route = mock.post("https://cb.example.com/hook").respond(200)
        client = CallbackClient("sign", get_metrics(), logging.getLogger("t"), max_retries=1)
        ok = await client.notify("https://cb.example.com/hook", {"run_id": "r1"})
        assert ok
        req = route.calls[0].request
        assert req.headers["X-Circus-Tent-Signature"].startswith("sha256=")


@pytest.mark.asyncio
async def test_callback_retry_then_failure() -> None:
    with respx.mock() as mock:
        route = mock.post("https://cb.example.com/hook").mock(
            side_effect=[respx.MockResponse(500), respx.MockResponse(200)]
        )
        client = CallbackClient("sign", get_metrics(), logging.getLogger("t"), max_retries=3)
        ok = await client.notify("https://cb.example.com/hook", {"a": 1})
        assert ok
        assert len(route.calls) == 2


# ---------------------------------------------------------------- preflight


class FakePage:
    def __init__(self, marker_present: bool) -> None:
        self.marker_present = marker_present
        self.url = ""

    async def goto(self, url: str, **kwargs) -> None:
        self.url = url

    async def evaluate(self, script: str) -> str:
        if "body" in script:
            return "signed-in marker" if self.marker_present else "login page"
        # pruning scripts receive the pruned text
        return "signed-in marker" if self.marker_present else "login page"


async def test_preflight_ok_and_expired() -> None:
    ok, reason = await check_session(FakePage(True), "https://x", "signed-in")
    assert ok and reason == ""
    ok, reason = await check_session(FakePage(False), "https://x", "signed-in")
    assert not ok and reason == "SESSION_EXPIRED"
    ok, reason = await check_session(FakePage(False), "", "")
    assert ok  # disabled
