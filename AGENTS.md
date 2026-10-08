# AGENTS.md — Instructions for Pi, Claude Code, Codex (and other coding agents)

## Project identity

You are working on **circus-tent**, a domain-isolated high-concurrency web-automation
wrapper. It runs isolated Camoufox browser shards per domain family, executes credentialed
step sequences against fortified enterprise portals (ATS: Workday, Greenhouse, Lever,
NEOGOV governmentjobs, plus Indeed and LinkedIn), extracts content with local deterministic
algorithms, and exposes REST + MCP interfaces. Python-native end-to-end. No TypeScript,
no cross-runtime serialization.

The binding contract is ARCHITECTURE.md ("Architectural Principles" and "Acceptance
Criteria"). Nothing in that contract may be relaxed for implementation convenience.

## Rules of engagement

1. **Read the spec first.** Every module directory under `modules/` contains
   `.omp-spec.md`. Read it fully before writing any code.
2. **One module at a time.** Do not modify files outside the module you are working on
   unless the spec explicitly allows it.
3. **No guessing business logic.** Selector semantics, tier behavior, and schema shapes
   are in `docs/contracts/`. Implement them exactly.
4. **Never commit secrets.** API keys, account credentials, and session cookies never
   enter git. `.env` is gitignored. Config files reference env var NAMES only.
   Profile dirs (`~/.automation-wrapper`) live outside the repo.
5. **Verification is mandatory.** Every module spec has a `verification_command`.
   Run it and make it pass before declaring done.
6. **Ask for clarification.** If a requirement is ambiguous, stop and ask the human.
   Do not invent behavior.
7. **Deterministic first.** Element selection, extraction, and recovery are local and
   deterministic. LLMs are invoked only for the heal and vision tiers. Never add an
   LLM call to a path that a cache, hash, or filter can serve.

## Project structure

```
circus-tent/
├── src/circus_tent/       # Python package (all code)
│   ├── browser/           # shards, fingerprints, cluster manager
│   ├── parser/            # DOM pruning, extraction pipeline
│   ├── engine/            # state machine, caches, ledger, budgets, rate limits
│   ├── api/               # FastAPI + FastMCP, fairness, auth, callbacks
│   ├── security/          # CapSolver challenge resolution
│   ├── config/            # env/config loading
│   ├── cli/               # circus-tent serve / bootstrap
│   └── telemetry.py       # OTel traces, Prometheus metrics, structured logs
├── modules/               # one .omp-spec.md per module (this is what you read)
├── config/                # shards.yaml, models.yaml, accounts.yaml, callers.yaml
├── docs/                  # contracts/ (API + schemas) and external/ (rate-limit research)
├── tests/                 # unit/ integration/ fixtures/
└── benchmarks/            # acceptance-criteria harness (§10)
```

Code lives under `src/circus_tent/<module>/`; the spec that governs it lives at
`modules/<module>/.omp-spec.md`.

## How to implement a module

1. `cat modules/<module>/.omp-spec.md`
2. Implement the files listed in IMPLEMENTATION CONTRACT. The skeleton file already
   exists with exact signatures and docstrings — keep signatures stable; fill bodies.
3. Do not add dependencies without editing `pyproject.toml` and explaining why in your
   completion report.
4. Write unit tests in `tests/unit/test_<module>_*.py`. No network, no browser in unit
   tests. Mock external IO.
5. Run the verification command from the spec. Iterate until it passes.
6. Report COMPLETED/FAILED/BLOCKED with the evidence (command output).

## Environment

- Python 3.14 (system interpreter), packages installed **system-wide** via uv:
  `uv sync --system` (see README). Do NOT create a venv.
- `uv.lock` is committed. After any dependency change: `uv lock && uv sync --system`.
- Lint: `ruff check .` — Must pass. Format with `ruff format`.
- Types: `mypy` (strict) — Must pass for `circus_tent` package code.
- Tests: `pytest` (asyncio auto mode). Markers: `unit`, `integration`, `benchmark`,
  `live`. CI/local default runs unit + integration (fixtures only).
- **Never run `live` tests in CI.** Live tests touch real external sites/accounts and
  are run manually only.

## Testing rules

- Unit tests: no network, no browser process. Mock via respx (HTTP) and monkeypatched
  browser stubs.
- Integration tests: use `tests/fixtures/` — captured real logged-out pages (pruned) and
  synthetic DOM mutants. The fixture-generation procedure is in
  `tests/fixtures/README.md`.
- Contract drift: a nightly, non-CI script re-validates fixtures against live targets
  and flags drift (`benchmarks/drift_check.py`). CI never touches live ATS endpoints.
- The heal benchmark mutates fixtures deterministically (seeded PRNG); never mutate a
  fixture in place.

## Cost discipline

- The heal and vision tiers cost tokens. Respect the budget engine
  (`circus_tent.engine.budgets`): per-session and per-shard daily caps, and the
  per-shard circuit breaker. Never bypass the budget checks.
- Prefer Muse Spark (cheapest per 1M tokens) for heal; only the vision tier uses the
  vision model. Never send a full-viewport screenshot to the vision model — localized
  crop + 100px padding only.
- Prompt templates are versioned and pinned per run (`config/models.yaml`).
  Changing a template requires bumping `prompt_templates.version` and a note in
  `docs/contracts/schemas.md`.

## Module index

| Module | Directory | Spec |
|---|---|---|
| config | src/circus_tent/config | modules/config/.omp-spec.md |
| browser | src/circus_tent/browser | modules/browser/.omp-spec.md |
| parser | src/circus_tent/parser | modules/parser/.omp-spec.md |
| engine | src/circus_tent/engine | modules/engine/.omp-spec.md |
| persistence | src/circus_tent/engine | modules/persistence/.omp-spec.md |
| llm | src/circus_tent/engine | modules/llm/.omp-spec.md |
| api | src/circus_tent/api | modules/api/.omp-spec.md |
| security | src/circus_tent/security | modules/security/.omp-spec.md |
| cli | src/circus_tent/cli | modules/cli/.omp-spec.md |
| telemetry | src/circus_tent/telemetry.py | modules/telemetry/.omp-spec.md |

## Documentation conventions

- Python 3.14 only. `from __future__ import annotations` is implied by the runtime.
- Strict mypy; no `Any` without a comment explaining why.
- asyncio end-to-end; no threads for browser or IO work.
- Structured JSON logs (via telemetry module), Prometheus metric names `circus_tent_*`.
- Public API responses and cache files carry `schema_version` fields; breaking changes
  require a migration entry in `docs/contracts/schemas.md`.
