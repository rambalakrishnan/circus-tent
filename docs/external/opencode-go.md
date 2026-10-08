# docs/external/opencode-go.md — OpenCode Go (LLM provider)

Source: https://opencode.ai/v2/docs/console/go (captured 2026-10-08).

## Plans

- **Go**: $10/month. **Go Plus**: $40/month (higher limits). Single-member workspaces only.
- Usage limits are monthly dollar amounts per model; per-model ceilings: 5-hour = 20%,
  weekly = 50%, monthly = 100% of that model's monthly limit.
- Go tier monthly limits: Muse Spark 1.3 Contributor $60; DeepSeek V4 Flash Vision Exp $15.
  Go Plus: $120 / $60 respectively.

## Models we bind

| Role | Model | Format | In/out per 1M | Monthly (Go / Go Plus) |
|---|---|---|---|---|
| text_healing | Muse Spark 1.3 Contributor | Responses API (`/zen/go/v1/responses`) | $0.10 / $0.20 | $60 / $120 |
| text_healing (fallback) | Muse Spark 1.2 Contributor | Responses API | $0.10 / $0.20 | $60 / $120 |
| vision_fallback | DeepSeek V4 Flash Vision Exp | chat/completions (`/zen/go/v1/chat/completions`) | $0.15 / $0.60 off-peak, $0.30 / $1.20 peak | $15 / $60 |

## Hard requirements for clients

1. **Custom User-Agent** identifying the client (we send `circus-tent/<version>`;
   configured in `config/models.yaml` `client_headers.user_agent`).
2. **Stable session header** `x-opencode-session` per conversation (we send a stable
   per-run UUID; `client_headers.session_header_name`).
3. Traffic is monitored for abuse that degrades service for other users.

## Caveats that shape our cost controls

- **Muse Spark 1.3 Contributor**: heavily discounted pricing in exchange for permission
  to use prompts and completions to train future Meta models; availability limited to
  regions permitted by Meta's Geographic Use Policy. Consequence: pruned-DOM content
  sent to the heal tier may be used for training. If that becomes unacceptable for a
  target, bind the heal role to a non-contributor model in `config/models.yaml` — the
  registry is externalized precisely for this. (Acknowledged with operator, 2026-10-08.)
- **DeepSeek ZDR** agreement renews monthly (valid through 2026-10-31 as of capture).
- 5-hour ceiling (20% of monthly): a runaway heal loop can exhaust the 5-hour budget
  in minutes → this is why the circuit breaker (30% heal-rate threshold) is mandatory,
  not optional.
- Peak/off-peak pricing on DeepSeek models: vision spend varies by time of day; the
  budgets engine caps tokens, not dollars; dollar tracking is via LiteLLM's pricing
  tables (approximate).

## Endpoints (as configured)

- Responses API: `https://opencode.ai/zen/go/v1/responses` (auth: `Authorization: Bearer <key>`)
- Chat completions: `https://opencode.ai/zen/go/v1/chat/completions` (same auth)
- Vision images are sent inline (base64 data URLs) in chat-completions messages.
- **Quirk (verified 2026-10-08):** /responses rejects `max_output_tokens < 16` with a
  400; model config keeps max_tokens ≥ 16. Both endpoints return `usage` blocks
  (input_tokens/output_tokens) — used for budget accounting.

## Rate limits

No published per-second RPM for the Go plan. Observed behavior: 429s on bursts;
the model client must treat 429 as retry-with-backoff and a trigger for provider
failover (both implemented). The 5-hour/weekly/monthly dollar ceilings are the binding
limits; monitor `circus_tent_tokens_*` metrics against them.
