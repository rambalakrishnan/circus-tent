# circus-tent

Domain-isolated high-concurrency web-automation wrapper over
[Camoufox](https://camoufox.com) + Playwright, with in-process Crawl4AI extraction,
self-healing selectors, and REST + MCP interfaces.

One isolated browser process per domain family (shard), pinned fingerprints, persisted
authenticated sessions (MFA included), bounded tab concurrency with fair-share session
quotas, and a Learn-Run-Heal state machine that keeps selector costs at zero tokens in
steady state.

Built for credentialed automation of fortified enterprise portals: Workday, Greenhouse,
Lever, NEOGOV governmentjobs, Indeed, LinkedIn.

**Not a general-purpose scraper.** Stateful, credentialed, bounded targets only.

## Quickstart

```bash
cd ~/workspace/circus-tent
cp .env.example .env      # fill in keys (or export them in the process env)
uv sync --system          # installs system-wide (Python 3.14); uv.lock is committed

circus-tent bootstrap --shard governmentjobs   # headful MFA handoff (once per shard)
circus-tent serve                               # 127.0.0.1:8000, MCP at /mcp
```

## Interfaces

REST (bearer `Authorization: Bearer $AUTOMATION_WRAPPER_API_KEY`):

| Method | Path | Purpose |
|---|---|---|
| POST | /run | Execute a step sequence (idempotency_key required) |
| POST | /extract | Extract content from a URL |
| GET | /health | Liveness + readiness |
| GET | /shards | Shard topology + utilization |
| GET | /metrics | Prometheus metrics |

MCP tools at `/mcp` (Streamable HTTP): `run`, `extract`, `health`, `list_shards`.

## Documentation

- `ARCHITECTURE.md` — the binding contract (principles + acceptance criteria)
- `AGENTS.md` — rules for coding agents (Pi, Claude Code, Codex)
- `modules/<name>/.omp-spec.md` — per-module implementation specs
- `docs/contracts/` — API + schema contracts (versioned)
- `docs/external/` — rate-limit / ToS / bot-posture research per external service

## Development

```bash
ruff check . && mypy && pytest              # unit + integration (fixtures only)
pytest -m benchmark                         # acceptance-criteria harness
pytest -m live                              # MANUAL only — touches real sites
```

Unit tests never touch network or a browser process. Integration tests run against
captured fixtures. CI never touches live ATS endpoints.

## Layout

```
src/circus_tent/    package: browser/, parser/, engine/, api/, security/, config/, cli/
config/             shards.yaml, models.yaml, accounts.yaml, callers.yaml (+ caches)
modules/            .omp-spec.md per module
docs/               contracts/ + external/
tests/              unit/ integration/ fixtures/
benchmarks/         acceptance-criteria harness
```
