# docs/external/target-sites.md — target posture research

robots.txt captured 2026-10-08 (UA: circus-tent-docs/0.1). No site publishes hard
per-minute rate limits; the operational ceilings below are conservative engineering
guidance, not vendor statements. All shards run credentialed sessions at
`request_rate_per_minute: 60` / `session_step_rate_per_minute: 20` with per-shard
backoff — see config/shards.yaml.

## Workday (workdayjobs.com / myworkday.com)

- robots.txt: `User-agent: * Allow: /` — fully open to crawlers. Publishes `/llms.txt`
  (LLM-Policy) — fetch at runtime; it documents permitted AI access.
- Anti-bot posture: strong. Fingerprint immutability matters most here (accounts are
  tenant-credentialed); session-validity preflight markers are tenant-specific and
  must be tuned per tenant (see shards.yaml note).
- Known patterns: Arkose/FunCaptcha on some tenants' auth flows; drag-and-drop resume
  drop zones (upload step mode `dnd`); 10+ page application wizards with
  save-and-continue → checkpoint steps are the norm, not the exception.
- Guidance: keep per-session step pacing human-scale (the default ceilings already
  are); never parallelize two tabs on the same tenant account.

## Greenhouse (greenhouse.io / boards.greenhouse.io)

- robots.txt (boards): only `/embed/` disallowed — very open.
- Anti-bot posture: moderate. Job boards are public pages; the dashboard side
  (credentialed) has standard session protections. No published rate limits.
- Guidance: boards.* pages are safe for higher RPM; keep credentialed dashboard
  traffic at default ceilings.

## Lever (jobs.lever.co)

- robots.txt: `Allow: /` with **Crawl-delay: 1** — the one target that states a pace
  expectation (≥1s between requests). Honor it: the lever shard's
  `request_rate_per_minute` should be ≤ 60 and the backoff policy must respect
  crawl-delay semantics (min-interval token bucket already enforces this at 60 RPM).
- Posture: moderate; public boards, standard session protections on the admin side.

## NEOGOV (governmentjobs.com)

- robots.txt: `User-agent: * Disallow: /` — generic bots fully blocked; only named
  search-engine bots allowed. This is a site that actively fences automation.
- Anti-bot posture: high. Login flows may carry Turnstile/hCaptcha; account-level
  lockouts for unusual patterns are common in gov hiring portals.
- Guidance: strictest pacing of all shards; preflight marker
  (`data-user-role` presence) tuned to the operator's account; CapSolver hooks likely
  to matter here first.

## Indeed (indeed.com)

- robots.txt: `Allow: /` with a long selective Disallow list (advanced_search, ads,
  rss, tracking params, etc.). Job pages themselves are allowed.
- Posture: sophisticated anti-automation on search/apply flows (Akamai-class edge
  defenses observed historically); apply flows sit behind login walls.
- Guidance: stay within allowed paths, respect the Disallow list strictly, default
  pacing, and expect edge challenges → CapSolver Turnstile/Akamai hooks enabled.

## LinkedIn (linkedin.com)

- robots.txt: "The use of robots or other automated means to access LinkedIn without
  the express permission of LinkedIn is strictly prohibited" — robots are disallowed
  broadly except LinkedInBot and selected search engines; crawling requires a
  whitelist agreement.
- **Operational note**: our framework's LinkedIn shard operates a single credentialed,
  human-paced session on the operator's own account — not anonymous crawling. That is
  a different posture from robot crawling, but LinkedIn's ToS and automated-access
  enforcement are aggressive; keep the linkedin shard at the most conservative pacing
  and disable parallel tabs on it by default (max_tabs override available in
  shards.yaml). This shard is highest lockout risk; treat every launch as potentially
  account-affecting.
- Guidance: max_tabs 1-2, step pacing human-scale, avoid burst navigation patterns.

## General engineering ceilings (per shard, defaults)

| Ceiling | Default | Where |
|---|---|---|
| requests/minute toward target | 60 | shards.yaml defaults.request_rate_per_minute |
| steps/minute per session | 20 | defaults.session_step_rate_per_minute |
| backoff triggers | 429/403/503/challenge | runtime backoff policy |
| backoff wait | 30s initial, 600s max | defaults.backoff |
| cooldown (page counter frozen) | 120s | defaults.backoff.cooldown_seconds |
| concurrent tabs | 12 (linkedin: consider 1-2) | shards.yaml |
| recycle threshold | 500 pages | defaults.recycle_after_pages |

These ceilings are deliberately conservative; raise them only with per-target
monitoring (429 rate, challenge rate, session-expiry rate) in place.
