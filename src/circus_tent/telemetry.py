"""Structured JSON logs, Prometheus metrics, OTel spans, secret redaction.

See modules/telemetry/.omp-spec.md.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from prometheus_client import Counter, Gauge, Histogram, generate_latest

_REDACTED = "***REDACTED***"

# --- Secret redaction -------------------------------------------------------

_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9]{20,}")
_AUTH_PATTERN = re.compile(r"(Authorization[\"']?\s*[:=]\s*[\"']?Bearer\s+)[A-Za-z0-9._~+/\-=]{1,}")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,}")
_SECRET_ENV_NAME = re.compile(r"SECRET|KEY|TOKEN|PASSWORD")


class SecretRedactor:
    """Pattern-based redaction for log values."""

    _registered_secrets: set[str] = set()

    def __init__(self, extra_patterns: Sequence[str] | None = None) -> None:
        self._extra_patterns: list[re.Pattern[str]] = [
            re.compile(pattern) for pattern in (extra_patterns or [])
        ]

    @classmethod
    def register_secret(cls, value: str) -> None:
        """Register a secret value; any token equal to it will be redacted."""
        if value and value.strip():
            cls._registered_secrets.add(value)

    def redact(self, text: str) -> str:
        result = _SK_PATTERN.sub(_REDACTED, text)
        result = _AUTH_PATTERN.sub(r"\1" + _REDACTED, result)
        result = _TOKEN_PATTERN.sub(self._replace_secret_token, result)
        for pattern in self._extra_patterns:
            result = pattern.sub(_REDACTED, result)
        return result

    @staticmethod
    def _replace_secret_token(match: re.Match[str]) -> str:
        if match.group(0) in SecretRedactor._registered_secrets:
            return _REDACTED
        return match.group(0)


REDACTOR = SecretRedactor()  # Module-level redactor used by the JSON formatter.


def redact_secret(value: str) -> str:
    """Register a secret value for redaction and return the redaction marker."""
    SecretRedactor.register_secret(value)
    return _REDACTED


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return REDACTOR.redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


# --- JSON logging -----------------------------------------------------------

_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class _RedactingJsonFormatter(logging.Formatter):
    """JSON-lines formatter: ts (ISO8601 UTC), level, logger, event, extras."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).isoformat()
        data: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_ATTRS and key not in data:
                data[key] = _redact_value(value)
        try:
            return json.dumps(data, default=repr)
        except Exception:
            # Non-serializable values: repr fallback, and never raise on the hot path.
            try:
                return json.dumps({key: repr(value) for key, value in data.items()})
            except Exception:
                return json.dumps(
                    {
                        "ts": ts,
                        "level": record.levelname,
                        "logger": record.name,
                        "event": "<unserializable log record>",
                    }
                )


class _RedactingStreamHandler(logging.Handler):
    """Writes one JSON line per record to the current sys.stderr, never raising."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            stream = sys.stderr
            stream.write(message + "\n")
            stream.flush()
        except Exception:
            # Logging must never block or raise on the hot path.
            pass


_setup_done = False
_env_secrets_scanned = False


def _register_env_secrets() -> None:
    """Register values of env vars whose names look secret-bearing. Runs once."""
    global _env_secrets_scanned
    if _env_secrets_scanned:
        return
    _env_secrets_scanned = True
    for name, value in os.environ.items():
        if value and _SECRET_ENV_NAME.search(name):
            SecretRedactor.register_secret(value)


def setup_logging(level: str = "INFO") -> None:
    """Install JSON-lines logging with redaction. Idempotent."""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True
    _register_env_secrets()
    root = logging.getLogger()
    root.setLevel(level)
    handler = _RedactingStreamHandler()
    handler.setFormatter(_RedactingJsonFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    if not _setup_done:
        setup_logging()
    return logging.getLogger(name)


# --- Prometheus metrics -----------------------------------------------------


class Metrics:
    """Prometheus metrics singleton. All names are circus_tent_*."""

    def __init__(self) -> None:
        self.tabs_active: Gauge = Gauge("circus_tent_tabs_active", "Active tab contexts", ["shard"])
        self.queue_depth: Gauge = Gauge("circus_tent_queue_depth", "Queued requests", ["shard"])
        self.permit_wait_seconds: Histogram = Histogram(
            "circus_tent_permit_wait_seconds", "Permit acquisition wait", ["shard"]
        )
        self.tokens_consumed: Counter = Counter(
            "circus_tent_tokens_consumed", "LLM tokens consumed", ["shard", "role", "model"]
        )
        self.heal_attempts: Counter = Counter(
            "circus_tent_heal_attempts", "Heal tier attempts", ["shard", "outcome"]
        )
        self.vision_attempts: Counter = Counter(
            "circus_tent_vision_attempts", "Vision tier attempts", ["shard", "outcome"]
        )
        self.circuit_breaker_state: Gauge = Gauge(
            "circus_tent_circuit_breaker_state",
            "Heal circuit breaker (0 closed, 1 open)",
            ["shard"],
        )
        self.callback_failures: Counter = Counter(
            "circus_tent_callback_failures", "Callback delivery failures"
        )
        self.audit_events: Counter = Counter("circus_tent_audit_events", "Audit records")
        self.pages_processed: Counter = Counter(
            "circus_tent_pages_processed", "Pages processed", ["shard"]
        )
        self.http_requests: Counter = Counter(
            "circus_tent_http_requests", "HTTP requests", ["method", "path", "status"]
        )


_metrics: Metrics | None = None


def get_metrics() -> Metrics:
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics


def metrics_text() -> str:
    """Latest Prometheus text exposition from the default registry."""
    return generate_latest().decode("utf-8")


# --- OpenTelemetry spans ----------------------------------------------------

#: Inbound W3C traceparent captured by `traceparent_from`; consumed by `start_span`.
_inbound_traceparent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "circus_tent_inbound_traceparent", default=None
)

#: Cached tracer; Any because OTel is imported lazily to keep the hot path safe.
_tracer: Any | None = None


def _get_tracer() -> Any:
    """Build (once) the OTel tracer; None if OTel is unavailable or init fails."""
    global _tracer
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    except Exception:
        return None
    provider: Any  # TracerProvider / ProxyTracerProvider; kept Any for version tolerance.
    try:
        provider = otel_trace.get_tracer_provider()
    except Exception:
        provider = TracerProvider()
        try:
            otel_trace.set_tracer_provider(provider)
        except Exception:
            try:
                provider = otel_trace.get_tracer_provider()
            except Exception:
                provider = TracerProvider()
    # No exporter by default; console span processor only when debugging.
    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
        with contextlib.suppress(Exception):
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    try:
        _tracer = provider.get_tracer("circus_tent")
    except Exception:
        return None
    return _tracer


def _remote_parent_context(parts: list[str]) -> Any:
    """Build a parent context carrying an inbound remote trace id / span id."""
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

    span_context = SpanContext(
        trace_id=int(parts[1], 16),
        span_id=int(parts[2], 16),
        is_remote=True,
        trace_flags=TraceFlags(int(parts[3], 16)),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


def start_span(
    name: str, attributes: Mapping[str, Any] | None = None
) -> contextlib.AbstractContextManager[Any]:
    """Start an OTel span (child of current context). No-op-safe."""
    tracer = _get_tracer()
    if tracer is None:
        return contextlib.nullcontext()
    try:
        from opentelemetry import trace as otel_trace

        parent_context = None
        inbound = _inbound_traceparent.get()
        if inbound:
            parts = inbound.split("-")
            if len(parts) == 4:
                parent_context = _remote_parent_context(parts)
        span = tracer.start_span(
            name,
            attributes=dict(attributes) if attributes else None,
            context=parent_context,
        )
        return cast(
            contextlib.AbstractContextManager[Any],
            otel_trace.use_span(span, end_on_exit=True),
        )
    except Exception:
        return contextlib.nullcontext()


def current_traceparent() -> str | None:
    """``00-<trace>-<span>-01`` of the current span, or None."""
    try:
        from opentelemetry.trace import get_current_span

        span = get_current_span()
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return None
        return f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}-01"
    except Exception:
        return None


def traceparent_from(headers: Mapping[str, str]) -> str | None:
    """Parse the inbound W3C ``traceparent`` header; return the canonical form.

    Returns ``00-<traceid>-<spanid>-01``, or None when the header is missing or
    malformed (never raises). The parsed value is remembered so `start_span`
    can attach child spans to the inbound trace.
    """
    raw: str | None = None
    for key, value in headers.items():
        if key.lower() == "traceparent":
            raw = value
            break
    canonical: str | None = None
    if raw:
        parts = raw.strip().split("-")
        if len(parts) == 4 and parts[0] == "00":
            trace_id, span_id = parts[1], parts[2]
            hexes = trace_id + span_id
            if (
                len(trace_id) == 32
                and len(span_id) == 16
                and all(char in "0123456789abcdefABCDEF" for char in hexes)
                and not all(char == "0" for char in trace_id)
                and not all(char == "0" for char in span_id)
            ):
                canonical = f"00-{trace_id}-{span_id}-01"
    _inbound_traceparent.set(canonical)
    return canonical
