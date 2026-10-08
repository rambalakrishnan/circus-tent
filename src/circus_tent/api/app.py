"""ASGI assembly: FastAPI + mounted FastMCP + lifespan shutdown (B7). See spec.

Graceful shutdown contract: on app shutdown the lifespan drains in-flight
steps, flushes contexts, closes browsers, persists caches/ledger, and sets the
module-level `drain_status` (0 = clean, 1 = forced). The CLI reads it for the
process exit code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from circus_tent.api.auth import AuthError
from circus_tent.api.fairness import QueueTooDeep
from circus_tent.api.mcp_server import build_mcp_server
from circus_tent.api.rest import router
from circus_tent.api.service import Service
from circus_tent.browser.rate_limiter import RateLimited
from circus_tent.engine.budgets import BudgetExceeded
from circus_tent.engine.models import ModelError
from circus_tent.engine.state_machine import IdempotencyConflict
from circus_tent.telemetry import get_logger, get_metrics, traceparent_from


def _install_error_handlers(app: FastAPI) -> None:
    """Map domain exceptions raised inside Service to the documented envelope.

    Without these, an exception raised below the route (e.g. AuthError from a
    scope check inside Service.run) escapes the middleware as a bare 500,
    contradicting docs/contracts/api.md.
    """

    def _response(
        status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"api_version": "v1", "error": {"code": code, "message": message}},
            headers=headers or {},
        )

    @app.exception_handler(AuthError)
    async def _handle_auth(request: Request, exc: AuthError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
        return _response(exc.status, exc.code, exc.message, headers)

    @app.exception_handler(QueueTooDeep)
    async def _handle_queue(request: Request, exc: QueueTooDeep) -> JSONResponse:
        return _response(
            429,
            "RATE_LIMITED",
            str(exc),
            {"Retry-After": "1", "X-Queue-Depth": str(exc.depth)},
        )

    @app.exception_handler(RateLimited)
    async def _handle_rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        remaining = max(1, int(exc.until - monotonic()))
        return _response(429, "RATE_LIMITED", str(exc), {"Retry-After": str(remaining)})

    @app.exception_handler(IdempotencyConflict)
    async def _handle_conflict(request: Request, exc: IdempotencyConflict) -> JSONResponse:
        return _response(409, "IDEMPOTENCY_CONFLICT", str(exc))

    @app.exception_handler(BudgetExceeded)
    async def _handle_budget(request: Request, exc: BudgetExceeded) -> JSONResponse:
        return _response(429, "BUDGET_EXCEEDED", str(exc))

    @app.exception_handler(ModelError)
    async def _handle_model(request: Request, exc: ModelError) -> JSONResponse:
        return _response(502, "HEAL_DISABLED", str(exc))


#: Set by the lifespan shutdown handler; the CLI reads it for the process exit
#: code (0 = clean drain, 1 = forced).
drain_status: int = 0


class _AuthMiddleware(BaseHTTPMiddleware):
    """Bearer auth + per-caller rate limiting on EVERY path (incl. /metrics)."""

    def __init__(self, app: object, service: Service) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.service = service

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        metrics = get_metrics()
        authorization = request.headers.get("authorization")
        try:
            caller = self.service.auth.authenticate(authorization)
        except AuthError as e:
            metrics.http_requests.labels(
                method=request.method, path=request.url.path, status=str(e.status)
            ).inc()
            return JSONResponse(
                status_code=e.status,
                content={"api_version": "v1", "error": {"code": e.code, "message": e.message}},
                headers={"WWW-Authenticate": "Bearer"} if e.status == 401 else {},
            )
        request.state.caller = caller
        try:
            await self.service.auth.check_rate(caller)
        except AuthError as e:
            metrics.http_requests.labels(
                method=request.method, path=request.url.path, status="429"
            ).inc()
            return JSONResponse(
                status_code=429,
                content={"api_version": "v1", "error": {"code": e.code, "message": e.message}},
                headers={"Retry-After": str(int(self.service.auth.retry_after(caller)) + 1)},
            )
        response = await call_next(request)
        metrics.http_requests.labels(
            method=request.method,
            path=request.url.path,
            status=str(getattr(response, "status_code", 500)),
        ).inc()
        return response


def create_app(
    config_dir: Path | str,
    env: Mapping[str, str] | None = None,
    warmup: bool = False,
) -> FastAPI:
    global drain_status
    service = Service(Path(config_dir), env)
    logger = get_logger("circus_tent.app")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start(warmup=warmup)
        logger.info("circus-tent started", extra={"event": "startup"})
        try:
            yield
        finally:
            logger.info("circus-tent draining", extra={"event": "shutdown_begin"})
            status = await service.shutdown(drain_seconds=30.0)
            drain_status = status  # noqa: F841  (read by the CLI via the module global)
            logger.info(
                "circus-tent stopped",
                extra={"event": "shutdown_end", "drain_status": status},
            )

    app = FastAPI(title="circus-tent", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.traceparent = traceparent_from
    _install_error_handlers(app)
    app.add_middleware(_AuthMiddleware, service=service)
    app.include_router(router)
    mcp = build_mcp_server(service)
    app.mount("/mcp", mcp.streamable_http_app())
    return app
