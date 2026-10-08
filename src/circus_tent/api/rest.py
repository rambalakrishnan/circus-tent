"""REST endpoints. See modules/api/.omp-spec.md and docs/contracts/api.md."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from circus_tent.api.auth import AuthError
from circus_tent.api.fairness import QueueTooDeep
from circus_tent.api.service import Service
from circus_tent.config.loader import CallerConfig
from circus_tent.engine.state_machine import IdempotencyConflict, RunResult
from circus_tent.telemetry import current_traceparent, get_metrics

router = APIRouter()

API_VERSION = "v1"


class StepModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal[
        "navigate", "fill", "click", "select", "wait", "extract", "assert", "upload", "checkpoint"
    ]
    url: str | None = None
    value: str | None = None
    selector_ref: str | None = None
    selector: str | None = None
    strategy: str = "css"
    fallback_text: str = ""
    side_effecting: bool = False
    milliseconds: int | None = None
    text: str | None = None
    query: str | None = None
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    file_ref: str | None = None
    mode: str = "direct"
    mime_types: tuple[str, ...] = ()
    max_bytes: int = 5_242_880
    note: str = ""


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    domain: str
    account: str | None = None
    session_id: str | None = None
    callback_url: str | None = None
    steps: list[StepModel] = Field(min_length=1)


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    url: str
    query: str | None = None
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    callback_url: str | None = None


def get_service(request: Request) -> Service:
    return cast(Service, request.app.state.service)


def get_caller(request: Request) -> CallerConfig:
    caller = getattr(request.state, "caller", None)
    if caller is None:
        raise AuthError(401, "UNAUTHORIZED", "no authenticated caller")
    return cast(CallerConfig, caller)


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"api_version": API_VERSION, "error": {"code": code, "message": message}},
        headers=headers or {},
    )


def _run_result_body(result: RunResult) -> dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "run_id": result.run_id,
        "idempotency_key": result.idempotency_key,
        "status": result.status,
        "resumed_from": result.resumed_from,
        # Wire contract per docs/contracts/api.md: step payloads (internal
        # extraction detail) are not exposed; the run-level `extracted` field is.
        "steps": [{k: v for k, v in asdict(s).items() if k != "payload"} for s in result.steps],
        "checkpoints": [],
        "stabilization_partial": result.stabilization_partial,
        "schema_incomplete": result.schema_incomplete,
        "extracted": result.extracted,
        "session": result.session,
    }


@router.post("/run")
async def run_endpoint(payload: RunRequest, request: Request) -> JSONResponse:
    service = get_service(request)
    caller = get_caller(request)
    req = payload.model_dump(by_alias=True)
    req["_traceparent"] = current_traceparent()
    try:
        result = await service.run(caller, req)
    except QueueTooDeep as e:
        return _error(
            429,
            "RATE_LIMITED",
            str(e),
            headers={"Retry-After": "1", "X-Queue-Depth": str(e.depth)},
        )
    except IdempotencyConflict as e:
        return _error(409, "IDEMPOTENCY_CONFLICT", str(e))
    return JSONResponse(content=_run_result_body(result))


@router.post("/extract")
async def extract_endpoint(payload: ExtractRequest, request: Request) -> JSONResponse:
    service = get_service(request)
    caller = get_caller(request)
    req = payload.model_dump(by_alias=True)
    req["_traceparent"] = current_traceparent()
    try:
        result = await service.extract(caller, req)
    except QueueTooDeep as e:
        return _error(
            429,
            "RATE_LIMITED",
            str(e),
            headers={"Retry-After": "1", "X-Queue-Depth": str(e.depth)},
        )
    return JSONResponse(
        content={
            "api_version": API_VERSION,
            "markdown": result.markdown,
            "structured": result.structured,
            "schema_incomplete": result.schema_incomplete,
            "stabilization_partial": bool(result.stabilization and result.stabilization.partial),
        }
    )


@router.get("/health")
async def health_endpoint(request: Request) -> JSONResponse:
    service = get_service(request)
    return JSONResponse(content=service.health())


@router.get("/shards")
async def shards_endpoint(request: Request) -> JSONResponse:
    service = get_service(request)
    return JSONResponse(content=service.shards())


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    get_metrics().http_requests.labels(method="GET", path="/metrics", status="200").inc()
    from circus_tent.telemetry import metrics_text

    return PlainTextResponse(content=metrics_text(), media_type="text/plain; version=0.0.4")
