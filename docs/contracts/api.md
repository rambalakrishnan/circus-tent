# docs/contracts/api.md — REST + MCP API contract

api_version: `v1` (schema_version field carried in request/response envelopes where noted)

## Authentication

Every call (REST and MCP) requires `Authorization: Bearer <key>`. Keys are resolved
per-caller from `config/callers.yaml` (`key_env`), compared in constant time.
Missing/invalid key → 401 with `WWW-Authenticate: Bearer`.

Per-caller scoping (B1): each caller is restricted to its `shards`, `tools`, `accounts`
allowlists, and its own `rate_limit_per_minute`. A scoped call → 403. Per-caller rate
limit exceeded → 429 with `Retry-After`. Every `/run` and `/extract` call writes an
audit record: ts, caller_id, tool, domain, account, idempotency_key, status.

## Back-pressure contract

When a session's fair-share quota is exhausted, the request is queued and the response
carries `Retry-After: <seconds>` and `X-Queue-Depth: <n>`. Queue depth > 1000 → HTTP 429
(same headers). No request is silently dropped: success, 429, or an explicit error.

## Common envelope

All JSON responses include `"api_version": "v1"` plus endpoint-specific fields.
Errors: `{"api_version": "v1", "error": {"code": "<CODE>", "message": "...", "details": {...}}}`.

Error codes: `BAD_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `SESSION_EXPIRED`,
`STABILIZATION_PARTIAL`, `SCHEMA_INCOMPLETE`, `SELECTOR_FAILED`, `VISION_FAILED`,
`HEAL_DISABLED`, `BUDGET_EXCEEDED`, `RATE_LIMITED`, `SHARD_UNAVAILABLE`,
`IDEMPOTENCY_CONFLICT`, `CALLBACK_FAILED`, `INTERNAL`.

## POST /run

Request:
```json
{
  "idempotency_key": "uuid",              // REQUIRED
  "domain": "workdayjobs.com",
  "account": "workday_acme",              // optional; defaults to shard default
  "session_id": "opaque",                 // informational
  "callback_url": "https://...",          // optional; terminal-state POST (C)
  "steps": [
    {"id": "s1", "type": "navigate", "url": "..."},
    {"id": "s2", "type": "fill", "selector_ref": "login_email", "value": "..."},
    {"id": "s3", "type": "click", "selector_ref": "submit", "side_effecting": false},
    {"id": "s4", "type": "upload", "mode": "direct|dnd", "file_ref": "file:///path/resume.pdf",
     "mime_types": ["application/pdf"], "max_bytes": 5242880},
    {"id": "s5", "type": "checkpoint", "note": "after wizard page 3"},
    {"id": "s6", "type": "assert", "selector_ref": "success", "text": "Submitted"},
    {"id": "s7", "type": "extract", "query": "job title", "schema": {"type": "object", ...}}
  ]
}
```

Step fields: `type` ∈ {navigate, fill, click, select, wait, extract, assert, upload,
checkpoint}; `side_effecting: true` marks at-most-once steps (submits, payments, account
creation, offer acceptance). Unknown/absent cached selectors: any step may instead carry
an inline `selector` + `strategy` + `fallback_text`.

Response:
```json
{
  "api_version": "v1",
  "run_id": "uuid",
  "idempotency_key": "uuid",
  "status": "completed|failed|resumed",
  "steps": [{"id": "s1", "status": "ok|healed|vision|failed|skipped", "tier": 1|2|3,
             "selector": "...", "error": null, "duration_ms": 120}],
  "checkpoints": [{"step_id": "s5", "index": 4, "created_at": "..."}],
  "stabilization_partial": false,
  "schema_incomplete": false,
  "extracted": {...},
  "session": {"preflight": "ok|SESSION_EXPIRED"}
}
```

Idempotency: re-POST with a known key resumes from the last checkpoint recorded in the
ledger (same shard + profile). Completed side-effecting steps are never re-executed;
a conflicting replay (different steps, same key, terminal state) → `IDEMPOTENCY_CONFLICT`.

Callbacks: on terminal state, POST `{run_id, idempotency_key, status, steps}` to
`callback_url` with `X-Circus-Tent-Signature: sha256=<hex hmac of body>` (key =
AUTOMATION_WRAPPER_API_KEY). Best-effort, 3 retries with backoff; failures surface in
`/health` and metrics.

## POST /extract

```json
{"url": "...", "domain": "workdayjobs.com", "query": "optional BM25 query",
 "schema": {"type": "object"}, "callback_url": "optional"}
```
Response: `{"api_version": "v1", "markdown": "...", "structured": {...} | null,
"schema_incomplete": bool, "stabilization_partial": bool, "shard": "workday"}`.

## GET /health

`{"api_version": "v1", "status": "ok", "shards": [{"name", "state", "tabs_active",
"pages_processed", "recycles", "fingerprint_ok", "cooldown_until": null}],
"queues": {"depth_total": 0}, "budgets": {...}, "circuit_breakers": {...},
"callback_failures": 0}`

## GET /shards

Shard topology + utilization: name, domain_patterns, state, tabs_active/max, profile
dir, fingerprint manifest status (present/absent), recycle counters.

## GET /metrics

Prometheus text format. `circus_tent_*` namespaces: tabs, queue depth, permit wait,
tokens per shard/model/day, heal/vision counts, circuit breaker state, callback
delivery, audit events.

## MCP tools (Streamable HTTP at /mcp)

- `run(domain, steps, idempotency_key, account?, session_id?, callback_url?)`
- `extract(domain, url, query?, schema?)`
- `health()` — same shape as GET /health
- `list_shards()` — same shape as GET /shards

MCP transport is Streamable HTTP only (WebSocket transport is deprecated and not used).

## Telemetry contract (C)

- Traces: OpenTelemetry; every request starts a span; W3C `traceparent` is honored on
  ingress and propagated to the callback POST.
- Logs: structured JSON (ts, level, event, caller_id, run_id, shard, trace_id).
  Values matching known secret patterns are redacted at the logging layer.
- Metrics: Prometheus on /metrics as above.
