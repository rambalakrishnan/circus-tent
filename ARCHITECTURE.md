# ARCHITECTURE.md — circus-tent

Version 1.0. This document supersedes the original technical brief. It is the binding
contract: "Architectural Principles" and "Acceptance Criteria" may not be relaxed; the
rest is implementation guidance. Decisions locked with the operator are recorded in
"Design Decisions" (§13).

---

## 1. Purpose & Scope

A production-grade web-automation framework for **high-concurrency, stateful interaction
with heavily fortified enterprise portals** — Applicant Tracking Systems (Workday,
Greenhouse, Lever, NEOGOV governmentjobs) and job boards (Indeed, LinkedIn).

The framework must:

- Execute hundreds of concurrent automation tasks without saturating host CPU, memory,
  or sockets.
- Mimic natural power-user traffic to avoid anti-bot detection.
- Persist authenticated sessions across runs, including MFA state.
- Extract structured content from SPAs without per-page LLM token costs.
- Self-heal when targets change DOM structure.
- Expose programmatic entry points (REST + MCP) usable by external orchestrators.

**Not** a general-purpose scraper; not for anonymous public crawling. It is a stateful,
credentialed harness for a bounded set of high-value targets.

## 2. Architectural Principles (binding)

1. **Domain isolation over single-browser multiplexing.** One isolated browser process
   per target domain family.
2. **Bounded parallelism over unbounded tab spawning.** Hard cap of concurrent tab
   contexts per shard; surge traffic is queued, never absorbed by flooding.
3. **Text-first intelligence.** Selection, navigation, recovery rely on semantic
   textual structure. Visual analysis is a late-stage fallback only.
4. **Local extraction over remote LLM extraction.** BM25 / cosine / markdown chunking
   first; LLMs only for self-healing and vision fallback.
5. **Fingerprint immutability per profile.** A profile's fingerprint never changes.
   Changing it requires a full profile reset and MFA re-handoff.
6. **Secrets live in the environment, never in git.** Config files reference env var
   names only. Dev convenience: `.env` at repo root (gitignored, chmod 600), loaded at
   startup when no richer source exists; production exports real env vars.
7. **Single-runtime implementation.** Python-native end-to-end. No cross-language
   serialization, no sidecar HTTP pipes, no dual-runtime state.

## 3. Technology Stack

| Layer | Technology | Role |
|---|---|---|
| Browser engine | Camoufox 0.5.7 (Python API) | Stealth Firefox, C++-level fingerprint injection |
| Browser driver | Playwright async API (bundled with Camoufox) | Tab/context lifecycle, navigation, evaluation |
| Extraction | Crawl4AI (in-process) | BM25 / pruning content filters, markdown generation |
| Text-healing model | Muse Spark 1.3 Contributor via OpenCode Go | Selector recovery from pruned DOM |
| Vision fallback model | DeepSeek V4 Flash Vision Exp via OpenCode Go | Bounding-box localization from screenshots |
| LLM conduit | LiteLLM | Token counting, budget enforcement, provider failover |
| API framework | FastAPI (ASGI / uvicorn) | REST endpoints + MCP server mounting |
| MCP server | mcp.server.fastmcp.FastMCP | Tools over Streamable HTTP (WebSocket transport forbidden) |
| Fairness / concurrency | asyncio primitives + custom fair-share semaphore | Tab slot distribution across sessions |
| Challenge resolution | CapSolver | Bot-wall bypass hooks; all task types ship enabled |
| Telemetry | OpenTelemetry (W3C traceparent) + prometheus-client + structlog-style JSON | Traces, metrics, structured logs |

**Why Python:** Camoufox's stealth lives at the C++ engine level and its Python API is
the reference implementation. Crawl4AI is Python-native. Single runtime eliminates
cross-language silent-failure risk and serialization boundaries.

## 4. System Architecture

```
                         External callers (MCP clients, REST consumers, orchestrators)
                                          │  HTTP / Streamable HTTP
┌─────────────────────────────────────────▼──────────────────────────────────────┐
│ API Layer (FastAPI + FastMCP, one ASGI app, 127.0.0.1:8000)                    │
│ /run /extract /health /shards /metrics   MCP at /mcp: run, extract, health,    │
│ list_shards                                bearer auth + per-caller scoping     │
│ idempotency_key on /run; optional callback_url; audit log per call             │
└─────────────────────────────────────────┬──────────────────────────────────────┘
                                          │
┌─────────────────────────────────────────▼──────────────────────────────────────┐
│ Fairness & Concurrency Layer                                                    │
│  shard Semaphore(12)  •  per-account quota (default 3)  •  fair-share FIFO      │
│  back-pressure: Retry-After + X-Queue-Depth headers, 429 at depth > 1000        │
│  per-caller rate limit (independent of session quotas)                          │
└─────────────────────────────────────────┬──────────────────────────────────────┘
                                          │
┌─────────────────────────────────────────▼──────────────────────────────────────┐
│ Cluster Manager                                                                 │
│  domain→shard routing (declaration order, misc="*" last)                        │
│  lifecycle: INITIALIZING→READY→DRAINING→TERMINATED                              │
│  recycle at pages_processed ≥ 500; same-profile boot AFTER old process exits    │
│  target-side rate limiter (RPM ceilings, 429/403/503/challenge backoff, cooldown)│
└──────┬────────────────────┬────────────────────┬───────────────────────────────┘
       │                    │                    │
┌──────▼─────┐      ┌───────▼──────┐      ┌──────▼────────┐
│ workday    │      │ greenhouse   │ ...  │ misc          │   (7 shards incl.
│ ≤12 tabs   │      │ ≤12 tabs     │      │ ≤12 tabs      │   governmentjobs,
│ pinned fp  │      │ pinned fp    │      │ pinned fp     │   indeed, linkedin)
└──────┬─────┘      └───────┬──────┘      └──────┬────────┘
       └────────────────────┼────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────────────────────┐
│ Execution Engine                                                                 │
│  preflight (session validity) → Learn-Run-Heal state machine                     │
│  step types: navigate fill click select wait extract assert upload checkpoint   │
│  Tier 1 run (cached selectors, zero tokens) / Tier 2 heal (text model) /        │
│  Tier 3 vision (localized crop → perceptual-hash coordinate cache)              │
│  idempotency ledger, checkpoints keyed by idempotency_key                       │
└───────────────────────────┬─────────────────────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────────────────────┐
│ Extraction Pipeline (in-process)                                                │
│  SPA stabilization (network idle 500ms / DOM quiet 300ms / text hydration)      │
│  in-browser pruning via page.evaluate() — EXTRACT ≤250KB, HEAL ≤50KB profiles   │
│  Shadow-DOM uncloaking + iframe frame flattening (recursive, cross-frame)       │
│  Crawl4AI AsyncWebCrawler.arun(html=...) → BM25/Pruning filters → markdown       │
│  deterministic schema-guided extraction (schema_incomplete flag, no LLM)        │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Component Specifications

### 5.1 Browser Cluster Manager (`src/circus_tent/browser/cluster_manager.py`)

- Registry of shards, each bound to a domain family and an isolated persistent
  profile directory under `~/.automation-wrapper/profiles/<name>/` (0700).
- Routes tasks to shards by domain pattern (declaration order, first match wins;
  `misc` last, matches `"*"`).
- Enforces `max_tabs` (default 12) per shard via asyncio.Semaphore.
- Tracks pages processed per shard; triggers recycling at `recycle_after_pages` (500).
- Emits `shard_recycled` events (old/new PIDs, page counts, elapsed time).
- Hosts the **target-side rate limiter** (B6): per-shard request-per-minute ceiling,
  per-session step ceiling, per-shard backoff on 429/403/503 or challenge pages,
  cooldown window during which the shard page counter is frozen.

Shard topology lives in `config/shards.yaml` (schema_version 1).

### 5.2 Fingerprint Persistence (`src/circus_tent/browser/fingerprint.py`)

- First init of a profile dir: resolve Camoufox launch options (canvas, WebGL, audio,
  navigator, screen, fonts, platform), serialize to `{profile_dir}/fingerprint.json`.
- Every later launch loads the manifest and passes the same options back to the
  launcher alongside `persistent_context=True`.
- **Manifest is immutable after first creation.** Any launch-parameter change requires
  a full profile reset: purge profile dir + manifest, re-authenticate via MFA handoff.
  Changing fingerprint without clearing cookies = "new device" signal = account lockout
  risk on most ATS platforms.
- Profile layout:
  ```
  profiles/<shard_name>/
  ├── fingerprint.json        # immutable launch-options manifest
  ├── user_data/              # Camoufox persistent context (cookies, storage)
  ├── manifest_created_at.txt
  └── health.json
  ```

### 5.3 Shard Lifecycle & Recycling (`src/circus_tent/browser/shard.py`)

States: `INITIALIZING → READY → DRAINING → TERMINATED`.

Init: resolve profile + manifest (create if absent) → launch Camoufox
(`persistent_context=True`, pinned manifest, `humanize=True`, configured headless) →
health check (navigate `about:blank`, page responds, fingerprint hash matches manifest)
→ READY.

Headless modes:
- **First-run / MFA handoff:** `headless=False`. Operator completes MFA + onboarding
  visually; state persists to `user_data/`.
- **Production:** `headless="virtual"` (Xvfb virtual framebuffer). Standard headless is
  detectable and not used on anti-bot-heavy sites.

Recycle (amended): trigger at `pages_processed >= recycle_after_pages`.
1. Stop routing new work to the shard (DRAINING).
2. Wait for in-flight tab contexts to complete (up to grace timeout, default 60s).
3. Close all contexts via the **flush sequence**: persist session state explicitly
   before closing each context, then close the browser.
4. **Wait for the old browser process to fully exit** (PID reaped). Firefox writes
   cookies/storage asynchronously; spawning a replacement against the same profile
   directory while the old process holds profile locks corrupts the profile.
5. Launch the replacement from the **same profile directory** with the **same
   fingerprint manifest** — no snapshot/copy of `user_data/` is made while a browser
   is alive.
6. Health-check the replacement; atomically swap the registry reference; route new
   traffic; READY.
7. Log recycle event with old/new PIDs, page counts, elapsed time.
Recycling never rotates fingerprints.

### 5.4 SPA Stabilization & In-Browser Pruning (`src/circus_tent/parser/trimmer.py`)

Stabilization before extraction: (1) network idle 500ms, (2) no mutations for 300ms via
MutationObserver, (3) configured text signatures present. On timeout: proceed with
warning + `stabilization_partial` flag.

Pruning runs inside the browser via `page.evaluate()` — multi-MB SPA strings never cross
a transport boundary. The pruning script MUST (amended):

- Shallow-clone the document, strip `<script> <style> <noscript> <iframe> <link> <meta>`.
- Convert `<img>/<svg>` to inline semantic markers (`[ICON:calendar]`,
  `[BUTTON:submit]`) preserving aria-label/role/title/alt.
- **Recursively uncloak open Shadow DOM** (`.shadowRoot` present) by inlining rendered
  content. `cloneNode` + `querySelectorAll` cannot see inside shadow trees — the
  traversal must explicitly check `.shadowRoot`.
- **Flatten nested frames**: iterate `page.frames()` and merge each frame's pruned DOM
  into the unified string (cross-origin frames contribute where readable; unreadable
  frames contribute a `[FRAME:<src>]` marker).
- Preserve `aria-label`, `role`, `title`, `alt` on retained elements.
- Collapse whitespace, drop empty text nodes, minify.

Profiles: `EXTRACT` ≤ 250KB (structural context preserved, feeds BM25) and `HEAL`
≤ 50KB (aggressive: strip layout divs, collapse wrappers, keep only interactive +
textual nodes; feeds the heal model).

### 5.5 Extraction Pipeline (`src/circus_tent/parser/extraction.py`)

Crawl4AI in-process — no sidecar, no HTTP pipe, no serialization boundary. Single-page:
BM25ContentFilter (user_query, threshold 1.2) when a query is supplied, else
PruningContentFilter (0.4); DefaultMarkdownGenerator; `cache_mode="BYPASS"`.
Batch: MemoryAdaptiveDispatcher with a memory ceiling. Schema-guided extraction is a
deterministic post-processing step (jsonschema-based heuristics over the markdown), not
an LLM call; unsatisfiable schemas return best-effort partial + `schema_incomplete`.

### 5.6 Execution Engine (`src/circus_tent/engine/`)

Step types (typed actions): `navigate`, `fill`, `click`, `select`, `wait`, `extract`,
`assert`, **`upload`** (B4), **`checkpoint`** (D4).

**Preflight (B3):** before every `/run`, navigate the shard's configured preflight URL
and assert the authenticated marker; absent marker → fail fast `SESSION_EXPIRED` +
`session_expired` event (operators subscribe for re-MFA escalation). Per-shard config
in `config/shards.yaml`.

**Tier 1 — Run (zero tokens):** load `config/selectors/{domain}.json`; execute cached
selector in try/except (TimeoutError, ElementNotInteractableError, PlaywrightError);
on success bump `hit_count` + `last_verified`.

**Tier 2 — Heal (text model):** on failure, capture HEAL-profile pruned DOM; invoke the
text-healing model with pruned DOM + step `fallback_text` + failed selector. Response
contract: strict JSON `{selector, strategy, confidence, reason}`; fortify with a
programmatic JSON repair layer (partial-json-parser, llm-json-repair) before one retry,
then escalate to Tier 3. On success: write replacement selector back to the cache with
decremented confidence + incremented `heal_count`; retry the native op.

**Tier 3 — Vision (coordinate fallback):** capture a **localized screenshot crop of the
failing container + 100px padding** (never the full viewport — visual token cost and
coordinate resolution both suffer); compute perceptual hash (dHash); consult
`config/coordinates/{domain}.json` (hash → coordinates) and click cached coordinates on
hit; on miss invoke the vision model, parse the returned **bounding box**, click its
center, write hash → box mapping to the cache.

**Upload steps (B4):** two modes — direct input (`page.set_input_files()`) and
drag-and-drop emulation (construct DataTransfer, dispatch dragenter/dragover/drop on
the drop zone; Workday uses drop zones not addressable via set_input_files alone).
File bytes come from caller-supplied references (filesystem paths or object-store URIs),
never from framework storage. Enforce per-upload size limit and MIME allowlist.

**Checkpoints (D4):** a `checkpoint` step records resumable state (step index, extracted
form values, URL) into the execution ledger keyed by `idempotency_key`. A `/run` with
an existing key resumes from the last checkpoint on the same shard + profile instead of
re-executing from the top. Multi-page wizards (Workday 10+ pages, save-and-continue)
resume mid-list.

**Idempotency (B2):** `idempotency_key` mandatory on `/run`. Per-key execution ledger
records terminal state. Steps marked `side_effecting: true` (submits, payments, account
creation, offer acceptance) carry **at-most-once** semantics: a retried key never
re-executes a completed side-effecting step.

**Cache re-validation:** a low-priority background task re-validates cached selectors
against live pages on a rolling schedule; failures are marked `stale: true` and demoted.

Selector cache schema (`config/selectors/{domain}.json`, schema_version 1):
`{domain, version, last_verified, steps: {id: {selector, strategy, confidence,
hit_count, heal_count, fallback_text, last_verified}}}`.
Coordinate cache schema (`config/coordinates/{domain}.json`, schema_version 1):
`{domain, entries: {phash: {x, y, width, height, viewport, step_id, hit_count,
created_at}}}`.

### 5.7 Model Registry & LLM Conduit (`src/circus_tent/engine/models.py`,
`src/circus_tent/engine/budgets.py`)

`config/models.yaml` externalizes all model bindings (env var names only). Roles:
`text_healing` (Muse Spark 1.3 Contributor, OpenAI Responses-API format via
`MUSE_API_BASE`), `vision_fallback` (DeepSeek V4 Flash Vision Exp, chat-completions
format via `DEEPSEEK_API_BASE`). Providers are **ordered fallback lists** (C): a
timeout or 5xx fails over to the next entry.

**LiteLLM is the conduit for every LLM call** (B9): token counting, per-call budget
checks, provider failover. Every request sends a custom User-Agent
(`circus-tent/<version>`) and a stable `x-opencode-session` header (OpenCode Go
requirement — see docs/external/opencode-go.md).

**Budgets & circuit breaker (B9):** per-session and per-shard daily token caps for text
and vision separately; a per-shard circuit breaker disables the heal tier when heal
rate exceeds 30% of steps in a rolling hour (redesign ⇒ human review, not more tokens);
`/metrics` counters for tokens per shard, per model, per day.

### 5.8 Interface Layer (`src/circus_tent/api/`)

One ASGI app (uvicorn, 127.0.0.1:8000): FastAPI REST + FastMCP mounted at `/mcp` via
`mcp.streamable_http_app()` (Streamable HTTP only; WebSocket transport is deprecated).

MCP tools: `run(domain, steps, session_id|account, idempotency_key, callback_url?)`,
`extract(domain, url, query, schema?)`, `health()`, `list_shards()`.

REST: `POST /run`, `POST /extract`, `GET /health`, `GET /shards`, `GET /metrics`
(Prometheus text format).

**Auth & scoping (B1):** every call authenticates with a bearer key (constant-time
compare). Per-caller scoping (`config/callers.yaml`): which shards a caller may target,
which tools it may invoke, which accounts it may act as; per-caller rate limit
independent of session quotas. Every `/run` and `/extract` call is written to an audit
log binding it to the authenticated caller identity.

**Callbacks (C):** optional `callback_url` on `/run` and `/extract`; on terminal state
the framework POSTs a signed summary (success/per-step status/error). Delivery is
best-effort with retry; failures surface in `/health`.

**Telemetry (C):** OpenTelemetry traces with W3C traceparent propagation, Prometheus
metrics (`circus_tent_*`), structured JSON logs with secret-pattern redaction (B8).

### 5.9 Fairness & Concurrency (`src/circus_tent/api/fairness.py`)

Layer 1: per-shard `asyncio.Semaphore(max_tabs)` (default 12). Layer 2: per-account
quota (default 3 concurrent tabs; keyed on the registered account — caller-supplied
session IDs are informational only). Fair-share FIFO permit distribution: no session
starves another; metrics for permit-acquisition wait time, queue depth, per-session
utilization.

Back-pressure: requests beyond quota are queued; responses carry `Retry-After` and
`X-Queue-Depth`; queue depth beyond 1000 → HTTP 429. No request is silently dropped.

### 5.10 Challenge Resolution (`src/circus_tent/security/challenge_resolver.py`)

CapSolver integration hooks: createTask/getTaskResult over the CapSolver API with the
key from `CAPSOLVER_API_KEY`. All task types ship enabled (targets are naively unknown):
reCAPTCHA v2/v3, hCaptcha (+Enterprise), FunCaptcha/Arkose, Turnstile, DataDome,
GeeTest, Imperva/Incapsula, Akamai, PerimeterX. Detection hook: challenge detection on
navigation (DOM signatures per type) triggers a solve; solved tokens are injected per
type (g-recaptcha-response, h-captcha-response, turnstile token, datadome cookie, …).
Solve calls are budgeted and audited.

### 5.11 Configuration & Secrets (`src/circus_tent/config/loader.py`)

Files: `config/shards.yaml`, `config/models.yaml`, `config/accounts.yaml`,
`config/callers.yaml` (+ auto-managed selector/coordinate caches). Every file carries
`schema_version`. `${VAR}` / `${VAR:-default}` references resolve from the process
environment at load time. `.env` at repo root is loaded only as a dev fallback when the
process env lacks the variables (dotenv semantics, no override). Required env vars:
`MUSE_API_KEY`, `MUSE_API_BASE`, `DEEPSEEK_API_KEY`, `DEEPSEEK_API_BASE`,
`CAPSOLVER_API_KEY`, `AUTOMATION_WRAPPER_API_KEY`, plus account vars referenced by
`config/accounts.yaml`. No secret value is ever written to a file by the framework.

## 6. Operational Procedures

### 6.1 MFA Handoff (first-run bootstrap)

`circus-tent bootstrap --shard <name>`: launches the shard headless=False, the operator
completes MFA + onboarding visually and confirms; state persists to `user_data/`;
fingerprint manifest written; clean termination. Later runs use `headless="virtual"`
and the pre-authenticated cache. Fingerprint manifest is created only at bootstrap.

### 6.2 Recycle Monitoring

`shard_recycled` events carry old/new PIDs, page counts, elapsed time. A recycle
frequency spike indicates a memory leak or target-side behavior change.

### 6.3 Selector Cache Review

Review caches periodically: steps with `confidence < 0.5` (active redesign) and
`heal_count > 10` (chronic instability — review fallback text or change schema).

### 6.4 Graceful Shutdown (B7)

On SIGTERM/SIGINT: stop accepting work → drain in-flight steps to a configurable
timeout → close every context via the flush sequence → close every browser process →
persist caches, ledger, metrics → exit with a status code that tells the supervisor
whether the drain completed cleanly (0 = clean, 1 = forced). Every shutdown path runs
the profile-safe flush; deploys must never corrupt profiles.

## 7. Acceptance Criteria (binding)

| # | Benchmark | Target |
|---|---|---|
| 1 | Fingerprint stability: 100 launches of a shard with pinned manifest; compare canvas/WebGL/audio/navigator/screen/font hashes | Identical on all 100 launches |
| 2 | Extraction throughput: 500 × 150KB HTML strings through in-process pipeline; p50/p95 + peak RSS | p95 < 200ms, peak RSS < 2GB |
| 3 | Heal success: mutate 20% of an ATS DOM fixture (class renames, reorders, wrapper divs); text-model selector recovery | > 85% first-attempt recovery |
| 4 | Concurrency fairness: 100 concurrent sessions × 20 tasks against one shard | Zero quota violations; max wait < 3× median |
| 5 | Recycle correctness: run to 500 pages, recycle; replacement inherits pinned fingerprint; old browser drains | Zero orphan processes; fingerprint unchanged |
| 6 | MFA persistence: complete handoff on fresh profile, then 20 automated sessions | Zero re-auth prompts |
| 7 | Back-pressure: 2× shard cap concurrent requests; 429s carry Retry-After + X-Queue-Depth | Every request succeeds or gets explicit back-pressure |
| 8 | Secret leakage: grep logs, configs, profiles for known test secrets | Zero matches |

CI runs 1(short form), 2, 3(mocked model), 4, 7, 8 against fixtures — never live sites.

## 8. Glossary

ATS, Camoufox, CDP, Crawl4AI, fingerprint manifest, heal, MFA, pruning profile
(EXTRACT/HEAL), run (tier), shard, SPA, Streamable HTTP, tab context — see brief §11.

## 9. Design Decisions (locked with the operator, 2026-10-08)

1. Repo `rambalakrishnan/circus-tent`, public, **no license file**, no CI, branch `main`.
2. Python 3.14 system interpreter; uv with committed uv.lock; system-wide install, no venv.
3. Production runs on this host directly (127.0.0.1:8000; MCP at /mcp).
4. Secrets: `.env` at repo root (gitignored, chmod 600) as dev fallback; runtime code
   resolves env vars; the framework itself never writes secret values to disk.
5. Model endpoints both on OpenCode Go: heal = Muse Spark 1.3 Contributor via
   `/zen/go/v1/responses` (Responses API); vision = DeepSeek V4 Flash Vision Exp via
   `/zen/go/v1/chat/completions`. Fallbacks: Muse Spark 1.2 Contributor (heal).
   All CapSolver task types ship enabled.
6. Shards v1: workday, greenhouse, lever, governmentjobs (NEOGOV), indeed, linkedin,
   misc. Single shared Workday shard (all tenants through one profile).
7. Live accounts today: NEOGOV + LinkedIn (bootstrap is operator-run, later).
8. Sessions for fairness = registered accounts (caller-supplied session_id is
   informational). Bearer API key auth with per-caller scoping + audit log.
9. Concurrency defaults confirmed: cap 12, quota 3, queue depth 1000,
   recycle 500 pages / 60s grace. ~100 concurrent sessions expected; queueing is
   the intended design.
10. Heal response: strict JSON + programmatic repair layer (partial-json-parser,
    llm-json-repair); heal writes cache immediately with decremented confidence.
    Schema-guided extraction stays deterministic (no LLM fallback).
11. Vision returns bounding boxes; framework clicks box center; localized crop +100px.
12. Doc conventions mirror job-hunter: AGENTS.md + per-module .omp-spec.md +
    docs/contracts/ + docs/external/ (incl. per-service rate-limit research).
13. Quality gates: ruff + mypy strict + pytest full suite. Per-module git commits.
14. Fixtures: captured real logged-out pages of all six targets + synthetic mutants;
    nightly drift check is non-CI; CI never touches live ATS endpoints.
15. Package `circus_tent`, CLI `circus-tent` (serve, bootstrap).
