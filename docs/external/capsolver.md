# docs/external/capsolver.md — CapSolver (challenge resolution)

Source: https://docs.capsolver.com/ (captured 2026-10-08).

## API shape (implemented in circus_tent.security.challenge_resolver)

- Base: `https://api.capsolver.com`
- Auth: `{"clientKey": "<CAPSOLVER_API_KEY>"}` in each request body.
- `POST /createTask` → `{taskId}` → poll `POST /getTaskResult` (2-3s interval,
  exponential backoff) until `status: ready`.
- Task types (all enabled in v1 per operator decision):
  - `ReCaptchaV2Task` / `ReCaptchaV2EnterpriseTask` / `ReCaptchaV2TaskProxyLess`
  - `ReCaptchaV3Task` / `ReCaptchaV3EnterpriseTask`
  - `HCaptchaTask` / `HCaptchaTaskProxyLess` (+ Enterprise variants)
  - `FunCaptchaTask` / `FunCaptchaTaskProxyLess` (Arkose)
  - `AntiTurnstileTaskProxyLess` (Cloudflare Turnstile)
  - `DataDomeTask`, `GeeTestTask`, `ImpervaTask`, `AkamaiBMPTask`, `PerimeterXTask`
  - `ImageToTextTask` (auxiliary)

## Pricing (pay-per-usage, per 1000 requests, as published)

reCAPTCHA v2 $0.5 · v2 Enterprise $1 · v3 $0.5 · v3 Enterprise $3 · GeeTest $0.5 ·
ImageToText $0.4 · Turnstile $3 · (hCaptcha/FunCaptcha similar tiers; confirm at
dashboard before heavy use). Volume packages with discounts exist.

## Concurrency / rate limits

Capsolver throttles per API key by plan tier (free tier: 1-2 concurrent tasks;
paid tiers: 10+ concurrent). We serialize solves per shard with a small asyncio
semaphore (default 2) and a retry/backoff on 429 or `ERROR_RATE_LIMIT`. Solve attempts
are budgeted (per-shard daily solve cap, default 100) and audited — a solve storm is a
signal the target added a new challenge type, not a license to burn budget.

## Cost discipline

A solve costs roughly 0.5-3 USD per 1000 — trivial per unit, expensive under a
detection loop. The challenge resolver must: (1) detect, (2) solve, (3) inject once,
(4) cache success per challenge sitekey for the session; never re-solve the same
challenge in a loop. A shard that triggers solves on >10% of navigations in a rolling
hour is flagged in /health (same pattern as the heal circuit breaker).
