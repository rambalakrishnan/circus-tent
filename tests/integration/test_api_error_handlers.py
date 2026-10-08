"""Integration tests: the app-level exception handlers (wire-contract envelope).

These cover the gap the delegated test agent reported: domain exceptions raised
*below* the route (inside Service) must produce the documented JSON envelope and
status code, not a bare HTTP 500. The delegated suite asserted CallerAuth in
isolation for this reason; these assert the ASGI mapping itself.

No network, no browser, no Service construction: a minimal FastAPI app with the
real handler installer and routes that raise each domain exception.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from circus_tent.api.app import _install_error_handlers
from circus_tent.api.auth import AuthError
from circus_tent.api.fairness import QueueTooDeep
from circus_tent.browser.rate_limiter import RateLimited
from circus_tent.engine.budgets import BudgetExceeded
from circus_tent.engine.models import ModelError
from circus_tent.engine.state_machine import IdempotencyConflict

pytestmark = pytest.mark.integration


def _app_with(raiser: object) -> FastAPI:
    app = FastAPI()
    _install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:  # type: ignore[return]
        raise raiser  # type: ignore[misc]

    return app


def _check(exc: Exception, status: int, code: str, headers: dict[str, str] | None = None) -> None:
    client = TestClient(_app_with(exc), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == status, f"{exc!r} -> {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["api_version"] == "v1"
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    for key, value in (headers or {}).items():
        assert resp.headers.get(key) == value


def test_scope_violation_maps_to_403_envelope() -> None:
    """The bug the delegated agent found: AuthError from a scope check became 500."""
    _check(
        AuthError(403, "FORBIDDEN", "caller 'narrow' may not target shard 'lever'"),
        403,
        "FORBIDDEN",
    )


def test_missing_bearer_maps_to_401_with_challenge_header() -> None:
    _check(
        AuthError(401, "UNAUTHORIZED", "missing bearer token"),
        401,
        "UNAUTHORIZED",
        {"WWW-Authenticate": "Bearer"},
    )


def test_caller_rate_limit_maps_to_429() -> None:
    _check(AuthError(429, "RATE_LIMITED", "caller 'x' rate limit exceeded"), 429, "RATE_LIMITED")


def test_queue_too_deep_carries_backpressure_headers() -> None:
    client = TestClient(_app_with(QueueTooDeep(1200, 1000)), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"
    assert resp.headers["X-Queue-Depth"] == "1200"
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_idempotency_conflict_maps_to_409() -> None:
    _check(
        IdempotencyConflict("key 'k' completed with a different step list"),
        409,
        "IDEMPOTENCY_CONFLICT",
    )


def test_budget_exceeded_maps_to_429() -> None:
    _check(BudgetExceeded("text", "session"), 429, "BUDGET_EXCEEDED")


def test_model_error_maps_to_502() -> None:
    _check(ModelError("all providers failed"), 502, "HEAL_DISABLED")


def test_shard_rate_limit_maps_to_429_with_retry_after() -> None:
    import time

    client = TestClient(
        _app_with(RateLimited("workday", time.monotonic() + 42.0)),
        raise_server_exceptions=False,
    )
    resp = client.get("/boom")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
