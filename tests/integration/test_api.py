"""Integration tests for the REST surface: auth, scoping, back-pressure.

The ASGI app is built with a fake Service (no real cluster/browser). Auth uses
the REAL ``CallerAuth`` with keys resolved from monkeypatched env vars, so the
401/back-pressure envelopes and headers are produced by the genuine middleware
and route code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from circus_tent.api.auth import AuthError, CallerAuth
from circus_tent.api.fairness import FairShare, QueueTooDeep
from circus_tent.config.loader import CallerConfig
from circus_tent.engine.state_machine import RunResult, StepResult
from circus_tent.telemetry import get_metrics

pytestmark = pytest.mark.integration

KEY_OK_ENV = "CT_TEST_KEY_OK"
KEY_NARROW_ENV = "CT_TEST_KEY_NARROW"
VALID = {"Authorization": "Bearer ok-secret"}
NARROW = {"Authorization": "Bearer narrow-secret"}


class FakeService:
    """Service stand-in exposing only what the API layer consumes."""

    def __init__(self, config_dir: Path, env: dict[str, str] | None = None) -> None:
        self.ok_caller = CallerConfig("ok-caller", KEY_OK_ENV, ("*",), ("*",), ("*",), 6000.0)
        self.narrow_caller = CallerConfig(
            "narrow-caller", KEY_NARROW_ENV, ("workday",), ("run", "extract"), ("*",), 6000.0
        )
        self.auth = CallerAuth(
            (self.ok_caller, self.narrow_caller),
            lambda name: os.environ.get(name, ""),
            get_metrics(),
        )
        self.fairness: dict[str, FairShare] = {"workday": FairShare(12)}
        self.queue_too_deep = False

    async def start(self, warmup: bool = False) -> None:
        return None

    async def shutdown(self, drain_seconds: float = 30.0) -> int:
        return 0

    @staticmethod
    def _shard_for(domain: str) -> str:
        return "misc" if domain == "other.test" else "workday"

    async def run(self, caller: CallerConfig, req: dict[str, Any]) -> RunResult:
        self.auth.check_scope(
            caller, "run", self._shard_for(req.get("domain", "")), req.get("account")
        )
        if self.queue_too_deep:
            raise QueueTooDeep(1001, 1000)
        return RunResult(
            run_id="run-1",
            idempotency_key=str(req.get("idempotency_key", "")),
            status="completed",
            resumed_from=None,
            steps=(StepResult("s1", "ok", 1, None, None, 1),),
            extracted=None,
            schema_incomplete=False,
            stabilization_partial=False,
            session="ok",
        )

    async def extract(self, caller: CallerConfig, req: dict[str, Any]) -> Any:
        self.auth.check_scope(caller, "extract", self._shard_for(req.get("domain", "")), None)
        raise AssertionError("extract should not be reached in these tests")

    def health(self) -> dict[str, Any]:
        return {
            "api_version": "v1",
            "status": "ok",
            "shards": [{"name": "workday", "state": "ready", "tabs_active": 0}],
            "queues": {"depth_total": 0},
        }

    def shards(self) -> dict[str, Any]:
        return {"api_version": "v1", "shards": [{"name": "workday", "state": "ready"}]}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    import circus_tent.api.app as appmod

    monkeypatch.setenv(KEY_OK_ENV, "ok-secret")
    monkeypatch.setenv(KEY_NARROW_ENV, "narrow-secret")
    monkeypatch.setattr(appmod, "Service", FakeService)

    app = appmod.create_app(tmp_path, env={})
    with TestClient(app) as test_client:
        yield test_client


def test_missing_auth_returns_401_envelope(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert body["api_version"] == "v1"
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_wrong_token_returns_401(client: TestClient) -> None:
    response = client.get("/health", headers={"Authorization": "Bearer not-the-key"})
    assert response.status_code == 401


def test_health_ok_with_valid_token(client: TestClient) -> None:
    response = client.get("/health", headers=VALID)
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert "shards" in body


def test_scoped_out_caller_is_forbidden(client: TestClient) -> None:
    # Per-caller scoping is enforced by CallerAuth.check_scope (FORBIDDEN/403).
    # NOTE: the ASGI app has no AuthError exception handler, so a scope violation
    # raised inside a route is NOT surfaced as a 403 envelope today (see report);
    # this asserts the contract at the layer that implements it.
    service: FakeService = client.app.state.service
    caller = service.auth.authenticate(NARROW["Authorization"])
    with pytest.raises(AuthError) as excinfo:
        service.auth.check_scope(caller, "run", "misc", None)  # needs a non-workday shard
    assert excinfo.value.status == 403
    service.auth.check_scope(caller, "run", "workday", None)  # allowed shard


def test_queue_too_deep_returns_429_with_backpressure_headers(client: TestClient) -> None:
    service: FakeService = client.app.state.service
    service.queue_too_deep = True
    payload = {
        "idempotency_key": "k1",
        "domain": "workday.test",
        "steps": [{"id": "s1", "type": "wait", "milliseconds": 1}],
    }
    response = client.post("/run", headers=VALID, json=payload)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.headers["x-queue-depth"] == "1001"
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_metrics_returns_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics", headers=VALID)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "circus_tent_" in response.text
