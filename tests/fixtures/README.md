# tests/fixtures/ — captured pages and synthetic mutants

## Fixture-generation procedure (amendment D2)

1. Record a live target's page ONCE (logged-out public pages only), here in
   `captured_raw/` (gitignored — raw captures are bulky and subject to drift).
2. Prune the raw HTML to the committed fixture (EXTRACT profile) under
   `tests/fixtures/` using the parser module's `prune_html`. Committed fixtures are
   small, deterministic, and never re-fetched by CI.
3. CI and local test runs use ONLY the committed fixtures. CI never touches live
   endpoints.
4. `benchmarks/drift_check.py` (nightly, non-CI, manual) re-fetches the URLs in
   `captured_raw/*.meta.txt`, compares HTTP status, `<title>`, and size, and flags
   drift so an operator can re-record. Drift = contract risk, not a test failure.

## Current fixture set (captured 2026-10-08, logged-out)

| File | Site | Status | Notes |
|---|---|---|---|
| greenhouse.html | boards.greenhouse.io/greenhouse | 200, 184KB | Full public board |
| lever.html | jobs.lever.co/lever | 200, 712KB | Full public board |
| governmentjobs.html | www.governmentjobs.com | 200, 74KB | Public landing |
| linkedin.html | www.linkedin.com | 200, 141KB | Authwall/login page |
| indeed.html | www.indeed.com/jobs?q=... | 403, 27KB | Bot-wall challenge page — useful for challenge-detection tests |
| workday_login.html | myworkday.com login | 200, 26KB | Richest curl-capturable Workday markup |
| workday_shell.html | www.workdayjobs.com | 200, 114B | Pre-hydration JS redirect shell |

Raw captures live in `captured_raw/` (gitignored); the committed fixtures are the
pruned EXTRACT/HEAL profiles plus these HTML originals where small enough
(< 200KB compressed is too large for some — see per-file presence).

## Synthetic mutants

For the heal benchmark (acceptance #3), mutants are generated deterministically from
the committed fixtures by `benchmarks/make_mutants.py` with a seeded PRNG: class
renames, element reordering, extra wrapper divs, attribute shuffling. Mutants are
written to a scratch dir at runtime, never committed, never mutating the fixtures
in place.
