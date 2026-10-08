# docs/contracts/schemas.md — versioned schemas & migration policy

Every persisted or exchanged artifact carries `schema_version` (or, for caches,
`version`). Breaking changes require a bump, a migration entry below, and a fallback
reader for the previous version wherever feasible.

## Migration policy

1. Non-breaking (additive field) → minor bump, old readers unaffected.
2. Breaking (field removed/renamed/type-changed) → major bump + migration function.
3. Prompt template changes (config/models.yaml `prompt_templates.version`) do not
   silently invalidate cached selectors: entries record the prompt version they were
   healed under; on version bump they are demoted to `stale` and re-validated, not
   deleted.

## Migration log

| From | To | Change | Date |
|---|---|---|---|
| — | 1 | Initial schemas | 2026-10-08 |

## config/shards.yaml (schema_version 1)

```yaml
schema_version: 1
profiles_root: path-with-env-refs
defaults: {max_tabs, recycle_after_pages, recycle_grace_seconds, headless,
           request_rate_per_minute, session_step_rate_per_minute, backoff{...}}
shards: [{name, domain_patterns[], profile_dir, preflight{url, marker}}]
```
Rules: declaration-order routing; `misc` last and `"*"`.

## config/models.yaml (schema_version 1)

```yaml
schema_version: 1
roles: {text_healing: {providers[]}, vision_fallback: {providers[]}}
provider: {name, api_base, api_key_env, api_format: responses|chat_completions,
           max_tokens, temperature, timeout_seconds}
budgets: {session_daily_text_tokens, session_daily_vision_tokens,
          shard_daily_text_tokens, shard_daily_vision_tokens,
          circuit_breaker{window_seconds, heal_rate_threshold, cooldown_seconds}}
client_headers: {user_agent, session_header_name}
prompt_templates: {version, heal, vision}
```

## config/accounts.yaml (schema_version 1)

```yaml
schema_version: 1
accounts: [{id, shard, username_env, password_env, tenant?, mfa_profile, quota}]
```
Values resolved from env at runtime; file never contains credentials.

## config/callers.yaml (schema_version 1)

```yaml
schema_version: 1
callers: [{id, key_env, shards[], tools[], accounts[], rate_limit_per_minute}]
```

## config/selectors/{domain}.json (schema_version 1)

```json
{
  "domain": "workdayjobs.com",
  "version": "2026.10",
  "prompt_version": 1,
  "last_verified": "ISO8601",
  "steps": {
    "<step_id>": {
      "selector": "css|xp",
      "strategy": "css|xpath",
      "confidence": 0.97,
      "hit_count": 1247,
      "heal_count": 3,
      "stale": false,
      "fallback_text": "Email Address",
      "last_verified": "ISO8601"
    }
  }
}
```
Lookup order: non-stale first, then by confidence desc. Background re-validation marks
failures `stale: true` (demotion, not deletion).

## config/coordinates/{domain}.json (schema_version 1)

```json
{
  "domain": "workdayjobs.com",
  "entries": {
    "<dhash-hex>": {"x": 412, "y": 587, "width": 140, "height": 40,
                    "viewport": [1440, 900], "step_id": "submit_application",
                    "hit_count": 18, "created_at": "ISO8601"}
  }
}
```
x/y = top-left of the element bounding box; the engine clicks the box center.

## Execution ledger (B2/D4; engine-internal, not on disk in git)

Entry per run: `{run_id, idempotency_key, account, shard, domain, steps[] with
terminal state, checkpoints[] {step_id, index, created_at}, side_effect_completed[],
created_at, updated_at}`. Stored under the profile dir (runtime state), never in the
repo. Retry with same key → resume from last checkpoint; completed side-effecting
steps marked at-most-once.

## Audit log (B1)

One JSON line per /run and /extract: `{ts, caller_id, tool, domain, account,
idempotency_key, status, duration_ms, trace_id}`.

## Prompt templates (D3)

Templates in config/models.yaml are versioned and pinned per run: heal/vision responses
are cached (selectors/coordinates) with the prompt version in effect; a version bump
demotes cached entries to stale. Template changes require: bump `version`, a migration
log entry above, and a benchmark-3 re-run.
