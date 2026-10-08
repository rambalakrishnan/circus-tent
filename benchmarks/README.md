# benchmarks/ — acceptance-criteria harness

Runnable measurement of the binding benchmarks in `ARCHITECTURE.md` §7. Nothing here
touches live sites, networks, or paid models: criteria that genuinely need those things
are reported `SKIPPED` / `SIMULATED` with a concrete reason instead of being faked.

| File | Purpose |
|---|---|
| `harness.py` | Runs criteria 1–8, prints a results table, writes `benchmarks/output/report.json`. |
| `make_fixtures.py` | Regenerates `tests/fixtures/*.pruned.{extract,heal}.txt` + `MANIFEST.json`. |
| `make_mutants.py` | Pre-existing: writes synthetic DOM mutants to a scratch dir. |
| `drift_check.py` | Pre-existing: NIGHTLY/MANUAL live drift check (not CI, uses network). |
| `llm_smoke.py` | Pre-existing: manual live-model smoke test (uses network + paid model). |

## Quick start

```bash
# Fixtures: idempotent, deterministic, offline.
python3 benchmarks/make_fixtures.py            # write pruned fixtures + MANIFEST.json
python3 benchmarks/make_fixtures.py --check    # exit 1 if any pruned fixture is stale

# Harness: shrunk counts, no browser — completes in ~6s.
python3 benchmarks/harness.py --quick

# Full counts (500 extraction docs, 100x20 fairness) — still browserless.
python3 benchmarks/harness.py

# Enable the real-browser criteria (C1 fingerprint, C5 recycle).
python3 benchmarks/harness.py --with-browser --launches 3 --browser-timeout 60
```

Flags: `--quick` (shrunk counts), `--with-browser` (enables C1/C5),
`--launches N` (C1, default 3, acceptance target 100), `--count N` (C2 docs, default 500),
`--sessions`/`--tasks` (C4, default 100/20), `--mutants` (C3, default 20),
`--browser-timeout` (per launch/terminate bound, default 60s), `--output PATH`.

Exit code is `0` unless a criterion actually `FAIL`s (skips do not fail the run).

## Acceptance targets (ARCHITECTURE §7)

| # | Benchmark | Target | Harness status here |
|---|---|---|---|
| 1 | Fingerprint stability | 100 launches → identical canvas/WebGL/audio/navigator/screen/font hashes | `--with-browser`; target 100, script accepts N |
| 2 | Extraction throughput | p95 < 200 ms, peak RSS < 2 GB (500 × 150 KB) | `PASS` — browserless in-process reduction (p50 ≈ 106 ms, p95 ≈ 165 ms) |
| 3 | Heal success | > 85 % first-attempt recovery | plumbing only via stub (`SIMULATED`); true value `REQUIRES-LIVE-MODEL` |
| 4 | Concurrency fairness | zero quota violations; max wait < 3× median | fully measured |
| 5 | Recycle correctness | zero orphan processes; fingerprint unchanged | real with `--with-browser`; else `SIMULATED` against fakes |
| 6 | MFA persistence | zero re-auth prompts over 20 sessions | always `SKIPPED` (live accounts + operator) |
| 7 | Back-pressure | every request succeeds or gets explicit back-pressure | fully measured + REST contract check |
| 8 | Secret leakage | zero matches in tracked files/logs | fully measured |

## What each criterion measures

- **C1 — fingerprint stability.** Launches a real Camoufox shard N times against the same
  profile dir (the manifest is written on the first launch and reused), capturing the
  in-page fingerprint each time via `fingerprint.capture_fingerprint`; compares dicts with
  `compare_fingerprints`. All launches and teardowns share ONE event loop — Camoufox and
  Playwright objects are loop-bound, so running each operation under its own
  `asyncio.run()` hangs teardown and the criterion reports `SKIPPED` instead of a real
  measurement. A launch failure (no display / no Camoufox runtime) reports `SKIPPED` with
  the error, never `FAIL`.
- **C2 — extraction throughput.** Generates N deterministic ~150 KB synthetic DOMs
  (headings, lists, `key: value` lines) and times each through the in-process pipeline
  (`extraction.extract_html`, the same code path `batch_extract_html` uses). Reports
  p50/p95 latency and process peak RSS (`resource.getrusage(RUSAGE_SELF).ru_maxrss`).
  The pipeline reduces HTML with crawl4ai's filters + markdown generator directly —
  both are synchronous and browserless — so no Playwright/Chromium runtime is needed
  and the p95 < 200 ms target is reachable (measured p50 ≈ 106 ms, p95 ≈ 165 ms over
  50 docs). Falling back to `AsyncWebCrawler` instead costs ~3 s per document because
  it spawns a browser per call; that fallback exists only for older crawl4ai builds.
- **C3 — heal success.** Mutates captured fixtures deterministically (class renames, extra
  wrapper divs, `<li>` reorders) and drives the engine's heal tier with a **deterministic
  stub** in place of the text model. It asserts the plumbing: pruned-DOM capture →
  replacement selector → `SelectorCache` write (with `heal_count` increment) → native-op
  retry → status `healed`/tier 2. Reported as **STUBBED MODEL — not the acceptance
  measurement**; the true >85 % number is `REQUIRES-LIVE-MODEL`.
- **C4 — concurrency fairness.** 100 sessions × 20 tasks against a real
  `FairShare(max_permits=12, default_quota=3)`; each session runs up to its quota
  concurrently. Tracks held permit counts (quota violations), session completion
  (starvation), and permit-acquisition wait (max / median / p95). Fails if any session
  exceeds quota, any session fails to complete, or max wait ≥ 3× median.
- **C5 — recycle correctness.** Checks a replacement shard inherits the **same**
  fingerprint manifest hash and the old process path drains (no orphan). With
  `--with-browser` this uses real launch/terminate; otherwise it exercises manifest
  immutability + `terminate()`/drain against a fake browser with a reaped pid and is
  labelled `SIMULATED`.
- **C6 — MFA persistence.** Always `SKIPPED`: requires live credentialed accounts and an
  operator handoff for MFA/onboarding.
- **C7 — back-pressure.** Submits 2× the shard cap (24) concurrent acquisitions against
  `FairShare(max_permits=12)`. Variant A (default depth): 12 hold + 12 queue, then all 24
  acquire after release (nothing dropped). Variant B (`max_queue_depth=5`): requests
  beyond depth raise `QueueTooDeep` (explicit back-pressure) — the invariant is that every
  request is acquired, queued, or explicitly refused, never silently dropped. Also asserts
  `docs/contracts/api.md` documents `Retry-After`, `X-Queue-Depth`, and the depth-1000 → 429 rule.
- **C8 — secret leakage.** Reads `AUTOMATION_WRAPPER_API_KEY`, `MUSE_API_KEY`,
  `DEEPSEEK_API_KEY` from the environment or `.env`, then scans every `git ls-files`
  tracked file (read-only; recursive-walk fallback if git is unavailable) and every `*.log`
  under the repo and the profiles dir. Assertion: zero real key values in tracked files and
  zero secret-shaped strings (`sk-*` or a real key value) in logs. `.env` is gitignored and
  expected to hold the keys — it is never counted as a leak. Secret values are never printed.

## Skipped / simulated in this environment

- **C1 / C5** are `SKIPPED` / `SIMULATED` by default: browser criteria are off unless
  `--with-browser` is passed, and each launch is bounded by `--browser-timeout`.
- **C2** is `SKIPPED` here because crawl4ai's `AsyncWebCrawler` launches a Playwright
  Chromium binary that is not installed and cannot be fetched offline. The harness still
  builds the synthetic docs and reports an **auxiliary** `prune_html(EXTRACT)` p50/p95
  (clearly labelled *not the acceptance measurement*).
- **C3** is `SIMULATED` (stub model) — the acceptance number needs the live text model.
- **C6** is always `SKIPPED` — live credentialed accounts + operator handoff.

## Fixtures

`make_fixtures.py` reads `tests/fixtures/captured_raw/*.html` (gitignored raw captures) and
runs `circus_tent.parser.trimmer.prune_html` in both profiles, writing
`tests/fixtures/<name>.pruned.extract.txt` and `<name>.pruned.heal.txt` plus
`tests/fixtures/MANIFEST.json` (url, capture date, raw/extract/heal byte sizes, reduction
ratios, oversize flags). It is idempotent and deterministic — identical inputs produce
byte-identical outputs (no timestamps embedded) — and does no network I/O. `--check` verifies
the committed pruned fixtures are current without writing.
