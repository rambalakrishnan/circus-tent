#!/usr/bin/env python3
"""Acceptance-criteria benchmark harness (ARCHITECTURE.md §7, criteria 1-8).

A single runnable script. It executes every acceptance criterion that can be
exercised in this environment and prints a results table, then writes
``benchmarks/output/report.json``. Criteria that genuinely require a live
browser, live credentialed accounts, or a paid model are reported as SKIPPED /
SIMULATED with a concrete reason — never faked as a pass.

No network access. No paid model calls (the heal criterion injects a
deterministic stub in place of the text model). Read-only: it does not modify
``src/`` and writes only to ``benchmarks/output/`` and scratch dirs.

Usage:
    python3 benchmarks/harness.py --quick
    python3 benchmarks/harness.py --with-browser --launches 5
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import re
import resource
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
RAW_DIR = FIXTURES / "captured_raw"
API_CONTRACT = REPO / "docs" / "contracts" / "api.md"
DEFAULT_REPORT = REPO / "benchmarks" / "output" / "report.json"

SCRATCH = Path(os.environ.get("TMPDIR", "/tmp")) / "circus-tent-bench"

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
SIMULATED = "SIMULATED"

#: Acceptance targets copied verbatim from ARCHITECTURE §7.
TARGETS = {
    "C1": "100 launches, fingerprints identical",
    "C2": "p95 < 200ms, peak RSS < 2GB",
    "C3": "> 85% first-attempt heal recovery",
    "C4": "zero quota violations; max wait < 3x median",
    "C5": "zero orphan processes; fingerprint unchanged",
    "C6": "zero re-auth prompts over 20 sessions",
    "C7": "every request succeeds or explicit back-pressure",
    "C8": "zero secret matches in tracked files/logs",
}

EXTRACT_LIMIT_BYTES = 250 * 1024


@dataclass
class Criterion:
    cid: str
    name: str
    target: str
    measured: str
    status: str
    notes: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- utils


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file WITHOUT mutating os.environ (no secret side effects)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _bench_shard_config(profile_dir: Path, headless: str = "virtual") -> Any:
    from circus_tent.config.loader import BackoffConfig, ShardConfig

    return ShardConfig(
        name="bench",
        domain_patterns=("*",),
        profile_dir=str(profile_dir),
        max_tabs=1,
        recycle_after_pages=500,
        recycle_grace_seconds=60.0,
        headless=headless,
        request_rate_per_minute=600.0,
        session_step_rate_per_minute=600.0,
        backoff=BackoffConfig(
            triggers=(429, 403, 503),
            initial_wait_seconds=1.0,
            max_wait_seconds=30.0,
            cooldown_seconds=60.0,
        ),
        preflight_url="",
        preflight_marker="",
    )


# --------------------------------------------------------------------------- C1


def c1_fingerprint_stability(args: argparse.Namespace, _ctx: dict[str, Any]) -> Criterion:
    target = f"{TARGETS['C1']} (target 100 allocations/launches)"
    if not args.with_browser:
        return Criterion(
            "C1",
            "fingerprint stability",
            target,
            "not run",
            SKIPPED,
            notes=(
                "browser criteria disabled; pass --with-browser to launch Camoufox. "
                f"--launches={args.launches} requested "
                "(acceptance target is 100 — hours of runtime)."
            ),
        )
    try:
        from circus_tent.browser.fingerprint import compare_fingerprints
        from circus_tent.browser.shard import Shard
        from circus_tent.telemetry import get_metrics
    except Exception as exc:  # noqa: BLE001
        return Criterion(
            "C1",
            "fingerprint stability",
            target,
            "import failed",
            SKIPPED,
            notes=f"{type(exc).__name__}: {exc}",
        )

    profile_dir = SCRATCH / "c1_profile"
    shutil.rmtree(profile_dir, ignore_errors=True)  # a killed run can leave a locked profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    cfg = _bench_shard_config(profile_dir, "virtual")
    manifest_hashes: list[str] = []
    snapshots: list[dict[str, Any]] = []
    completed: list[int] = [0]

    async def _launch_loop() -> None:
        """All launches (and their teardowns) must share ONE event loop.

        Camoufox/Playwright objects are bound to the loop that created them;
        running initialize() and terminate() under separate asyncio.run() calls
        makes teardown hang (the wrapper then SIGKILLs at the wall-clock budget
        and the criterion reports SKIPPED). Verified: a single-loop sequence of
        3 launches gives identical fingerprints and clean exits.
        """
        for _ in range(args.launches):
            shard = Shard(cfg, profile_dir, get_metrics(), logging.getLogger("bench"))
            await asyncio.wait_for(shard.initialize(), timeout=args.browser_timeout)
            try:
                snapshots.append(dict(shard._fp_snapshot))
                manifest_hashes.append(str(shard._manifest.manifest_hash))
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(shard.terminate(), timeout=args.browser_timeout)
            completed[0] += 1

    try:
        asyncio.run(asyncio.wait_for(_launch_loop(), timeout=args.browser_timeout * args.launches))
    except Exception as exc:  # noqa: BLE001
        return Criterion(
            "C1",
            "fingerprint stability",
            target,
            f"launch failed ({completed[0]} ok)",
            SKIPPED,
            notes=(
                "browser could not launch (no display / no Camoufox runtime): "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    differing: set[str] = set()
    for snap in snapshots[1:]:
        differing |= set(compare_fingerprints(snapshots[0], snap))
    status = PASS if not differing and len(set(manifest_hashes)) <= 1 else FAIL
    return Criterion(
        "C1",
        "fingerprint stability",
        target,
        f"{len(snapshots)} launches, {len(differing)} differing keys",
        status,
        notes=(
            f"manifest hash stable: {len(set(manifest_hashes)) <= 1}; "
            f"acceptance needs {args.launches}=100 (script accepts N)."
        ),
        detail={"differing_keys": sorted(differing), "manifest_hashes": manifest_hashes},
    )


class _suppress_bg:
    """Suppress exceptions from best-effort async cleanup."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


# --------------------------------------------------------------------------- C2


def _synthetic_html(index: int, target_bytes: int = 150_000) -> str:
    rng = random.Random(10_000 + index)
    parts = ["<html><head><title>Bench Page</title></head><body>"]
    size = sum(len(p) for p in parts)
    i = 0
    while size < target_bytes:
        i += 1
        block = [
            f"<h2>Section {i}</h2>",
            "<ul>"
            + "".join(
                f"<li>Item {i}-{j}: deterministic description text</li>"
                for j in range(rng.randint(3, 8))
            )
            + "</ul>",
            f"<p>Reference: REF-{rng.randint(1000, 9999)}</p>",
            f"<p>Location: Region {rng.randint(1, 40)}</p>",
            f"<div>Detail {i}: value {rng.random():.6f}</div>",
        ]
        blob = "".join(block)
        parts.append(blob)
        size += len(blob)
    parts.append("</body></html>")
    return "".join(parts)


def _c2_aux_prune(docs: list[str]) -> dict[str, float]:
    """Offline auxiliary: prune_html (EXTRACT) CPU cost only — NOT the acceptance measure."""
    from circus_tent.parser.trimmer import prune_html

    async def run() -> list[float]:
        lat: list[float] = []
        for doc in docs:
            t = time.perf_counter()
            await prune_html(doc, "EXTRACT")
            lat.append((time.perf_counter() - t) * 1000.0)
        return lat

    lat = asyncio.run(run())
    return {"p50_ms": round(_pct(lat, 0.5), 2), "p95_ms": round(_pct(lat, 0.95), 2)}


def c2_extraction_throughput(args: argparse.Namespace, _ctx: dict[str, Any]) -> Criterion:
    target = f"{TARGETS['C2']} over {args.count} x 150KB"
    docs = [_synthetic_html(i) for i in range(args.count)]
    avg_kb = round(sum(len(d) for d in docs) / len(docs) / 1024, 1)

    try:
        from circus_tent.parser.extraction import extract_html
    except Exception as exc:  # noqa: BLE001
        return Criterion(
            "C2",
            "extraction throughput",
            target,
            "import failed",
            SKIPPED,
            notes=f"{type(exc).__name__}: {exc}",
        )

    # Smoke test one document to detect a missing browser engine (crawl4ai launches one).
    try:
        asyncio.run(extract_html(docs[0]))
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        aux = _c2_aux_prune(docs)
        return Criterion(
            "C2",
            "extraction throughput",
            target,
            f"{args.count} x {avg_kb}KB, pipeline unavailable",
            SKIPPED,
            notes=(
                "in-process pipeline (crawl4ai) could not run: "
                f"{type(exc).__name__}: {reason}. crawl4ai's AsyncWebCrawler launches a "
                "Playwright Chromium binary (not installed; no network to fetch it), so the "
                "acceptance measurement cannot be produced here. Auxiliary prune_html(EXTRACT) "
                f"over the same docs: p50={aux['p50_ms']}ms p95={aux['p95_ms']}ms (NOT acceptance)."
            ),
            detail={"aux_prune": aux, "avg_kb": avg_kb},
        )

    async def run() -> list[float]:
        sem = asyncio.Semaphore(8)
        lat: list[float] = []

        async def one(html: str) -> None:
            async with sem:
                t = time.perf_counter()
                try:
                    await extract_html(html)
                finally:
                    lat.append((time.perf_counter() - t) * 1000.0)

        await asyncio.gather(*(one(d) for d in docs))
        return lat

    lat = asyncio.run(run())
    p50, p95 = _pct(lat, 0.5), _pct(lat, 0.95)
    rss = _peak_rss_mb()
    ok = p95 < 200.0 and rss < 2048.0
    return Criterion(
        "C2",
        "extraction throughput",
        target,
        f"p50={p50:.1f}ms p95={p95:.1f}ms RSS={rss:.0f}MB ({args.count} docs)",
        PASS if ok else FAIL,
        detail={"p50_ms": round(p50, 2), "p95_ms": round(p95, 2), "peak_rss_mb": round(rss, 1)},
    )


# --------------------------------------------------------------------------- C3

_CLASS_RE = re.compile(r'class="([^"]*)"', re.IGNORECASE)
_MARKER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*-m\d{3}")
_SUFFIX_RE = re.compile(r"-m\d{3}$")


def _mutate(html: str, rng: random.Random) -> tuple[str, int]:
    """Deterministically mutate a fixture: rename classes, then wrap OR reorder."""

    def rename(m: re.Match[str]) -> str:
        tokens = m.group(1).split()
        return 'class="' + " ".join(f"{t}-m{rng.randint(100, 999)}" for t in tokens) + '"'

    out = _CLASS_RE.sub(rename, html)
    kind = rng.randint(0, 2)
    if kind == 1 and "<div" in out:
        idx = out.find("<div", rng.randrange(max(1, len(out) // 4)))
        end = out.find(">", idx)
        close = out.rfind("</div>")
        if end > 0 and close > end:
            out = (
                out[: end + 1]
                + '<div data-mutant-wrapper="1">'
                + out[end + 1 : close]
                + "</div>"
                + out[close:]
            )
    elif kind == 2:
        chunks = re.findall(r"<li>.*?</li>", out)
        if len(chunks) >= 2:
            a, b = rng.sample(range(len(chunks)), 2)
            first, second = chunks[a], chunks[b]
            out = out.replace(first, "@@SWAP@@", 1).replace(second, first, 1)
            out = out.replace("@@SWAP@@", second, 1)
    return out, kind


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    def _op(self, kind: str) -> None:
        cls = self._selector[1:] if self._selector.startswith(".") else self._selector
        if cls not in self._page.classes:
            raise RuntimeError(f"selector not found in DOM: {self._selector}")
        self._page.ops.append((self._selector, kind))

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self._op("fill")

    async def click(self, timeout: int | None = None) -> None:
        self._op("click")

    async def select_option(self, label: str | None = None, timeout: int | None = None) -> None:
        self._op("select")


class _FakePage:
    def __init__(self, dom_html: str, classes: set[str]) -> None:
        self.dom_html = dom_html
        self.classes = classes
        self.ops: list[tuple[str, str]] = []

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def evaluate(self, script: str) -> str:
        return self.dom_html

    def frames(self) -> list[Any]:
        return []


class _FakeShard:
    def __init__(self, name: str = "bench") -> None:
        self.name = name


class _FakeCluster:
    def shard_for_domain(self, domain: str) -> _FakeShard:
        return _FakeShard()


class _FakeBudgets:
    def circuit_open(self, shard: str) -> bool:
        return False

    def record_step_outcome(self, shard: str, healed: bool) -> None:
        return None


class _StubHealer:
    """Deterministic STUB text model: recovers the mutated class from the pruned DOM.

    It never calls a model. It mirrors what a real healer would return
    (``{selector, strategy, confidence, reason}``) so the ENGINE PLUMBING can be
    measured without a paid call.
    """

    async def heal(self, pruned_dom: str, fallback_text: str, failed_selector: str) -> Any:
        from circus_tent.engine.models import HealResponse

        match = _MARKER_RE.search(pruned_dom)
        if match is None:
            return HealResponse(selector=None, strategy=None, confidence=0.0, reason="no marker")
        return HealResponse(
            selector="." + match.group(0), strategy="css", confidence=0.8, reason="stub recovery"
        )


def _run_heal_case(
    base_html: str, rng: random.Random, case_dir: Path
) -> tuple[bool, dict[str, Any]]:
    from circus_tent.engine.selector_cache import SelectorCache
    from circus_tent.engine.state_machine import (
        CoordinateCacheStore,
        ExecutionEngine,
        SelectorCacheStore,
        Step,
    )
    from circus_tent.parser.trimmer import prune_html
    from circus_tent.telemetry import get_metrics

    mutated, kind = _mutate(base_html, rng)
    classes = {tok for m in _CLASS_RE.finditer(mutated) for tok in m.group(1).split()}
    marker = _MARKER_RE.search(mutated)
    if marker is None:
        return False, {"reason": "no marker class after mutation"}
    marker_class = marker.group(0)
    original = _SUFFIX_RE.sub("", marker_class)
    cached_selector = "." + original  # no longer present -> Tier 1 fails
    correct_selector = "." + marker_class
    if original == marker_class or cached_selector in {(".", *classes)}:
        return False, {"reason": "mutation produced no recoverable selector"}

    domain = "greenhouse.io"
    cache_key = "field"
    cache_dir = case_dir / "selectors"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "greenhouse.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "domain": domain,
                "steps": {
                    cache_key: {
                        "selector": cached_selector,
                        "strategy": "css",
                        "confidence": 0.9,
                        "hit_count": 0,
                        "heal_count": 0,
                        "fallback_text": "Email address",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    page = _FakePage(mutated, classes)
    store = SelectorCacheStore(cache_dir, prompt_version=1)
    engine = ExecutionEngine(
        cluster=_FakeCluster(),
        caches=store,
        coord_caches=CoordinateCacheStore(case_dir / "coords"),
        model_client=_StubHealer(),
        ledger=object(),
        budgets=_FakeBudgets(),
        challenge_resolver=None,
        metrics=get_metrics(),
        logger=logging.getLogger("bench"),
    )
    step = Step(
        id="s1", type="fill", selector_ref=cache_key, fallback_text="Email address", value="a@b.c"
    )
    import types

    tc = types.SimpleNamespace(page=page)
    result = asyncio.run(engine._interactive(tc, step, domain))

    cache: SelectorCache = store.for_domain(domain)
    entry = cache.get(cache_key)
    cache.save()
    reloaded = SelectorCache(cache_dir / "greenhouse.json", domain, 1)
    reloaded.load()
    persisted = reloaded.get(cache_key)
    retried = any(sel == correct_selector and op == "fill" for sel, op in page.ops)

    ok = (
        result.status == "healed"
        and result.tier == 2
        and result.selector == correct_selector
        and entry is not None
        and entry.selector == correct_selector
        and entry.heal_count == 1
        and persisted is not None
        and persisted.selector == correct_selector
        and retried
    )
    # canned pruned DOM must be non-trivial for a real model (sanity)
    pruned_len = len(asyncio.run(prune_html(mutated, "HEAL")))
    return ok, {
        "kind": kind,
        "status": result.status,
        "tier": result.tier,
        "cached_selector": cached_selector,
        "correct_selector": correct_selector,
        "heal_count": entry.heal_count if entry else None,
        "retried_native_op": retried,
        "pruned_len": pruned_len,
    }


def c3_heal_success(args: argparse.Namespace, ctx: dict[str, Any]) -> Criterion:
    target = f"{TARGETS['C3']} (20% of an ATS DOM mutated)"
    all_bases = sorted(RAW_DIR.glob("*.html"))
    # A mutant needs a class-bearing DOM to rename; JS-redirect shells have none.
    bases = [
        p
        for p in all_bases
        if any(m.group(1).split() for m in _CLASS_RE.finditer(p.read_text(errors="replace")))
    ]
    if not bases:
        return Criterion(
            "C3",
            "heal success",
            target,
            "no usable fixtures",
            SKIPPED,
            notes=f"no class-bearing captured fixtures under {RAW_DIR}",
        )
    rng = random.Random(4242)
    successes = 0
    cases: list[dict[str, Any]] = []
    for i in range(args.mutants):
        base = bases[i % len(bases)].read_text(encoding="utf-8", errors="replace")
        case_dir = SCRATCH / "c3" / f"case{i}"
        case_dir.mkdir(parents=True, exist_ok=True)
        try:
            ok, info = _run_heal_case(base, rng, case_dir)
        except Exception as exc:  # noqa: BLE001
            ok, info = False, {"error": f"{type(exc).__name__}: {exc}"}
        successes += int(ok)
        cases.append({**info, "base": bases[i % len(bases)].name})
    rate = successes / args.mutants if args.mutants else 0.0
    notes = (
        "STUBBED MODEL — not the acceptance measurement. A deterministic in-process stub "
        "replaces the text-healing model; only the heal TIER PLUMBING is measured (pruned DOM "
        "capture -> replacement selector -> SelectorCache write incl. heal_count -> native-op "
        "retry). The true >85% first-attempt recovery criterion is REQUIRES-LIVE-MODEL."
    )
    return Criterion(
        "C3",
        "heal success (selector recovery)",
        target,
        f"mechanical recovery {successes}/{args.mutants} ({rate:.0%}), stubbed model",
        SIMULATED,
        notes=notes,
        detail={
            "cases": cases,
            "mechanical_recovery_rate": round(rate, 4),
            "bases_used": [p.name for p in bases],
        },
    )


# --------------------------------------------------------------------------- C4


async def _c4_run(sessions: int, tasks: int, quota: int, permits: int) -> dict[str, Any]:
    from circus_tent.api.fairness import FairShare

    fs = FairShare(max_permits=permits, default_quota=quota)
    lock = asyncio.Lock()
    held: dict[str, int] = defaultdict(int)
    violations = 0
    max_held = 0
    waits: list[float] = []
    completed: set[str] = set()

    async def one_task(session: str) -> None:
        nonlocal violations, max_held
        t = time.monotonic()
        await fs.acquire(session)
        waits.append(time.monotonic() - t)
        async with lock:
            held[session] += 1
            max_held = max(max_held, held[session])
            if held[session] > quota:
                violations += 1
        await asyncio.sleep(0.0005)
        async with lock:
            held[session] -= 1
        fs.release(session)

    async def one_session(session: str) -> None:
        sem = asyncio.Semaphore(quota)  # a session never exceeds its own quota

        async def wrapped() -> None:
            async with sem:
                await one_task(session)

        await asyncio.gather(*(wrapped() for _ in range(tasks)))
        completed.add(session)

    await asyncio.gather(*(one_session(f"acct-{i}") for i in range(sessions)))
    return {
        "sessions": sessions,
        "tasks_per_session": tasks,
        "total_tasks": sessions * tasks,
        "quota_violations": violations,
        "max_concurrent_per_session": max_held,
        "sessions_completed": len(completed),
        "max_wait_ms": round(max(waits) * 1000, 3) if waits else 0.0,
        "median_wait_ms": round(statistics.median(waits) * 1000, 3) if waits else 0.0,
        "p95_wait_ms": round(_pct(waits, 0.95) * 1000, 3) if waits else 0.0,
    }


def c4_concurrency_fairness(args: argparse.Namespace, _ctx: dict[str, Any]) -> Criterion:
    target = f"{TARGETS['C4']} ({args.sessions} sessions x {args.tasks} tasks)"
    try:
        res = asyncio.run(asyncio.wait_for(_c4_run(args.sessions, args.tasks, 3, 12), timeout=120))
    except TimeoutError:
        return Criterion(
            "C4",
            "concurrency fairness",
            target,
            "timed out",
            FAIL,
            notes="FairShare did not drain within 120s (starvation/deadlock?)",
        )
    except Exception as exc:  # noqa: BLE001
        return Criterion(
            "C4",
            "concurrency fairness",
            target,
            "error",
            FAIL,
            notes=f"{type(exc).__name__}: {exc}",
        )

    med = res["median_wait_ms"]
    maxw = res["max_wait_ms"]
    ratio_ok = (med <= 0) or (maxw < 3.0 * med)
    no_starve = res["sessions_completed"] == args.sessions
    ok = res["quota_violations"] == 0 and no_starve and ratio_ok
    measured = (
        f"viol={res['quota_violations']} maxWait={maxw:.1f}ms "
        f"medWait={med:.1f}ms ratio={maxw / med if med else 0:.1f}x"
    )
    return Criterion(
        "C4",
        "concurrency fairness",
        target,
        measured,
        PASS if ok else FAIL,
        notes=(
            f"no starvation: {res['sessions_completed']}/{args.sessions} sessions completed; "
            f"max wait < 3x median: {ratio_ok}."
        ),
        detail=res,
    )


# --------------------------------------------------------------------------- C5


class _FakeBrowser:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return int(proc.pid)


def c5_recycle_correctness(args: argparse.Namespace, _ctx: dict[str, Any]) -> Criterion:
    target = TARGETS["C5"]
    from circus_tent.browser.fingerprint import build_launch_options, resolve_manifest
    from circus_tent.telemetry import get_metrics

    profile_dir = SCRATCH / "c5_profile"
    shutil.rmtree(profile_dir, ignore_errors=True)  # a killed run can leave a locked profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    cfg = _bench_shard_config(profile_dir, "virtual")

    if args.with_browser:
        try:
            from circus_tent.browser.shard import Shard

            first = Shard(cfg, profile_dir, get_metrics(), logging.getLogger("bench"))
            asyncio.run(asyncio.wait_for(first.initialize(), timeout=args.browser_timeout))
            hash1 = str(first._manifest.manifest_hash)
            pid1 = first._pid()
            asyncio.run(asyncio.wait_for(first.terminate(), timeout=args.browser_timeout))
            orphan = _pid_alive(pid1)
            replacement = Shard(cfg, profile_dir, get_metrics(), logging.getLogger("bench"))
            asyncio.run(asyncio.wait_for(replacement.initialize(), timeout=args.browser_timeout))
            hash2 = str(replacement._manifest.manifest_hash)
            pid2 = replacement._pid()
            asyncio.run(asyncio.wait_for(replacement.terminate(), timeout=args.browser_timeout))
            ok = hash1 == hash2 and not orphan and pid1 != pid2
            return Criterion(
                "C5",
                "recycle correctness",
                target,
                f"hash stable={hash1 == hash2} orphan={orphan} pid swap={pid1 != pid2}",
                PASS if ok else FAIL,
                notes="real Camoufox launch/terminate path",
                detail={"manifest_hash": hash1, "old_pid": pid1, "new_pid": pid2},
            )
        except Exception as exc:  # noqa: BLE001
            return Criterion(
                "C5",
                "recycle correctness",
                target,
                "browser launch failed",
                SKIPPED,
                notes=f"Camoufox could not launch: {type(exc).__name__}: {exc}",
            )

    # --- SIMULATED: manifest immutability + drain/orphan logic against fakes ---
    first_manifest = resolve_manifest(profile_dir, build_launch_options(profile_dir, "virtual"))
    replacement_manifest = resolve_manifest(
        profile_dir,
        build_launch_options(profile_dir, "true"),  # different req -> ignored
    )
    inherits = first_manifest.manifest_hash == replacement_manifest.manifest_hash

    from circus_tent.browser.shard import Shard

    dead = _dead_pid()
    shard = Shard(cfg, profile_dir, get_metrics(), logging.getLogger("bench"))
    fake_browser = _FakeBrowser(dead)
    shard._browser = fake_browser
    asyncio.run(shard.terminate())  # waits for the (already-reaped) pid; must not hang
    drained = fake_browser.closed and shard.state.value == "terminated"
    orphan = _pid_alive(dead)

    ok = inherits and drained and not orphan
    return Criterion(
        "C5",
        "recycle correctness",
        target,
        f"manifest stable={inherits} drained={drained} orphan={orphan}",
        SIMULATED if ok else FAIL,
        notes=(
            "SIMULATED (no browser): fingerprint manifest immutability proves the replacement "
            "inherits the SAME hash; terminate()/drain logic exercised against a fake browser "
            "with an already-reaped pid. A real launch is REQUIRES-BROWSER (--with-browser)."
        ),
        detail={
            "manifest_hash": first_manifest.manifest_hash,
            "replacement_inherits_same_hash": inherits,
            "drained": drained,
            "orphan": orphan,
        },
    )


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- C6


def c6_mfa_persistence(_args: argparse.Namespace, _ctx: dict[str, Any]) -> Criterion:
    return Criterion(
        "C6",
        "MFA persistence",
        TARGETS["C6"],
        "not run",
        SKIPPED,
        notes="requires live credentialed accounts + operator handoff",
    )


# --------------------------------------------------------------------------- C7


async def _c7_run(cap: int = 12) -> dict[str, Any]:
    from circus_tent.api.fairness import FairShare, QueueTooDeep

    # Variant A — default queue depth: 2x cap, 12 hold + 12 queue, then drain.
    fs = FairShare(max_permits=cap)
    acquired: list[int] = []
    raised_a = 0

    async def acquire_default(i: int) -> None:
        nonlocal raised_a
        try:
            await fs.acquire(f"k{i}")
            acquired.append(i)
        except QueueTooDeep:
            raised_a += 1

    tasks = [asyncio.create_task(acquire_default(i)) for i in range(2 * cap)]
    await asyncio.sleep(0.05)
    depth_at_saturation = fs.queue_depth()
    held_at_saturation = len(acquired)
    for i in list(acquired):
        fs.release(f"k{i}")
    await asyncio.gather(*tasks)

    # Variant B — shallow queue: requests beyond depth get explicit QueueTooDeep.
    fs2 = FairShare(max_permits=cap, max_queue_depth=5)
    outcome = {"acquired": 0, "queued": 0, "too_deep": 0, "other": 0}

    async def acquire_shallow(_i: int) -> None:
        try:
            await fs2.acquire(f"j{_i}")
            outcome["acquired"] += 1
        except QueueTooDeep:
            outcome["too_deep"] += 1
        except Exception:  # noqa: BLE001
            outcome["other"] += 1

    tasks2 = [asyncio.create_task(acquire_shallow(i)) for i in range(2 * cap)]
    await asyncio.sleep(0.05)
    for t in tasks2:
        if not t.done():
            outcome["queued"] += 1
            t.cancel()
    await asyncio.gather(*tasks2, return_exceptions=True)

    return {
        "cap": cap,
        "variant_a": {
            "requests": 2 * cap,
            "held_at_saturation": held_at_saturation,
            "queue_depth_at_saturation": depth_at_saturation,
            "acquired_eventually": len(acquired),
            "queue_too_deep": raised_a,
        },
        "variant_b": {**outcome, "requests": 2 * cap},
    }


def c7_back_pressure(_args: argparse.Namespace, _ctx: dict[str, Any]) -> Criterion:
    target = f"{TARGETS['C7']} (2x shard cap = 24 concurrent)"
    try:
        res = asyncio.run(asyncio.wait_for(_c7_run(12), timeout=60))
    except TimeoutError:
        return Criterion(
            "C7",
            "back-pressure",
            target,
            "timed out",
            FAIL,
            notes="FairShare acquire set did not settle within 60s",
        )
    except Exception as exc:  # noqa: BLE001
        return Criterion(
            "C7", "back-pressure", target, "error", FAIL, notes=f"{type(exc).__name__}: {exc}"
        )

    va, vb = res["variant_a"], res["variant_b"]
    drained = va["acquired_eventually"] == va["requests"] and va["queue_too_deep"] == 0
    sat = va["held_at_saturation"] == 12 and va["queue_depth_at_saturation"] == 12
    shallow_ok = (
        vb["acquired"] + vb["queued"] + vb["too_deep"] == vb["requests"]
        and vb["other"] == 0
        and vb["too_deep"] > 0
    )

    contract_text = API_CONTRACT.read_text(encoding="utf-8") if API_CONTRACT.is_file() else ""
    rest_headers = {
        "Retry-After": "Retry-After" in contract_text,
        "X-Queue-Depth": "X-Queue-Depth" in contract_text,
        "429_at_depth_1000": "1000" in contract_text,
    }
    rest_ok = all(rest_headers.values())

    ok = drained and sat and shallow_ok and rest_ok
    measured = (
        f"A: held={va['held_at_saturation']} queued={va['queue_depth_at_saturation']} "
        f"final={va['acquired_eventually']}; B: deep={vb['too_deep']}"
    )
    return Criterion(
        "C7",
        "back-pressure",
        target,
        measured,
        PASS if ok else FAIL,
        notes=(
            "every request acquired, queued (back-pressure), or explicitly refused with "
            f"QueueTooDeep; docs/contracts/api.md retry semantics present: {rest_ok} "
            f"{rest_headers}."
        ),
        detail={"variant_a": va, "variant_b": vb, "rest_contract": rest_headers},
    )


# --------------------------------------------------------------------------- C8

_SECRET_ENV_NAMES = ("AUTOMATION_WRAPPER_API_KEY", "MUSE_API_KEY", "DEEPSEEK_API_KEY")
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")


def _tracked_files() -> tuple[list[str], str]:
    """git ls-files (read-only) with a recursive-walk fallback."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, timeout=30, check=False
        )
        if out.returncode == 0 and out.stdout.strip():
            return [line for line in out.stdout.splitlines() if line], "git ls-files"
    except Exception:  # noqa: BLE001
        pass
    walk = sorted(
        str(p.relative_to(REPO)) for p in REPO.rglob("*") if p.is_file() and ".git" not in p.parts
    )
    return walk, "recursive walk (git unavailable)"


def _secret_values() -> dict[str, str]:
    dotenv = _read_env_file(REPO / ".env")
    values: dict[str, str] = {}
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name) or dotenv.get(name) or ""
        if value.strip():
            values[name] = value
    return values


def c8_secret_leakage(_args: argparse.Namespace, ctx: dict[str, Any]) -> Criterion:
    target = TARGETS["C8"]
    secrets = _secret_values()

    tracked, method = _tracked_files()
    leaks: list[dict[str, str]] = []
    scanned = 0
    for rel in tracked:
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        scanned += 1
        for name, value in secrets.items():
            if value.encode() in data:
                leaks.append({"file": rel, "name": name})

    # Logs: *.log under the repo and under the profiles dir.
    profiles_dir = ctx["profiles_dir"]
    log_paths = list(REPO.rglob("*.log"))
    if profiles_dir is not None and profiles_dir.is_dir():
        log_paths += list(profiles_dir.rglob("*.log"))
    log_hits: list[dict[str, str]] = []
    logs_scanned = 0
    for log_path in log_paths:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        logs_scanned += 1
        for name, value in secrets.items():
            if value in text:
                log_hits.append({"file": str(log_path), "name": name})
        if _SK_RE.search(text):
            log_hits.append({"file": str(log_path), "name": "sk-* pattern"})

    env_has_secrets = bool(secrets)
    ok = not leaks and not log_hits
    measured = (
        f"{scanned} tracked files scanned, {len(leaks)} leaks; "
        f"{logs_scanned} logs scanned, {len(log_hits)} hits"
    )
    return Criterion(
        "C8",
        "secret leakage",
        target,
        measured,
        PASS if ok else FAIL,
        notes=(
            f"file selection: {method}. Keys checked: {sorted(secrets) or 'none present'}. "
            f".env contains secrets (expected; gitignored): {env_has_secrets}. "
            "Scanned only tracked files + *.log; secret values never printed."
        ),
        detail={
            "tracked_method": method,
            "tracked_files_scanned": scanned,
            "tracked_leaks": leaks,
            "logs_scanned": logs_scanned,
            "log_hits": log_hits,
            "keys_checked": sorted(secrets),
        },
    )


# --------------------------------------------------------------------------- driver

CRITERIA = [
    ("C1", c1_fingerprint_stability),
    ("C2", c2_extraction_throughput),
    ("C3", c3_heal_success),
    ("C4", c4_concurrency_fairness),
    ("C5", c5_recycle_correctness),
    ("C6", c6_mfa_persistence),
    ("C7", c7_back_pressure),
    ("C8", c8_secret_leakage),
]


def _resolve_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="circus-tent acceptance-criteria harness (§7)")
    parser.add_argument("--quick", action="store_true", help="shrink counts for a fast run")
    parser.add_argument("--with-browser", action="store_true", help="enable C1/C5 browser launches")
    parser.add_argument(
        "--launches", type=int, default=None, help="C1 launches (default 3; target 100)"
    )
    parser.add_argument("--count", type=int, default=None, help="C2 docs (default 500)")
    parser.add_argument("--sessions", type=int, default=None, help="C4 sessions (default 100)")
    parser.add_argument("--tasks", type=int, default=None, help="C4 tasks/session (default 20)")
    parser.add_argument("--mutants", type=int, default=None, help="C3 mutants (default 20)")
    parser.add_argument(
        "--browser-timeout",
        type=float,
        default=60.0,
        help="per-launch/terminate timeout for C1/C5 (default 60s) — a browserless host SKIPs",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--_browser-child",
        default=None,
        choices=["C1", "C5"],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    args.launches = args.launches if args.launches is not None else 3
    args.count = args.count if args.count is not None else (50 if args.quick else 500)
    args.sessions = args.sessions if args.sessions is not None else (10 if args.quick else 100)
    args.tasks = args.tasks if args.tasks is not None else (5 if args.quick else 20)
    args.mutants = args.mutants if args.mutants is not None else (6 if args.quick else 20)
    return args


def _profiles_dir() -> Path | None:
    try:
        from circus_tent.config.loader import ConfigLoader

        loader = ConfigLoader(REPO / "config")
        shards = loader.load_shards()
        return Path(shards[0].profile_dir).parent
    except Exception:  # noqa: BLE001
        return Path.home() / ".automation-wrapper"


def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_table(criteria: list[Criterion]) -> None:
    caps = (6, 28, 34, 44, 22)
    rows = [("C", "criterion", "target", "measured", "status")]
    for c in criteria:
        rows.append((c.cid, c.name, c.target, c.measured, c.status))
    widths = [min(caps[i], max(len(_trunc(r[i], caps[i])) for r in rows)) for i in range(len(caps))]
    line = "  ".join("-" * w for w in widths)
    print(line)
    for idx, row in enumerate(rows):
        cells = [_trunc(row[i], widths[i]).ljust(widths[i]) for i in range(len(widths))]
        print("  ".join(cells))
        if idx == 0:
            print(line)
    print(line)


_CRITERIA_BY_ID = dict(CRITERIA)
_CHILD_MARKER = "__CT_BROWSER_CRITERION__"


def _browser_budget(cid: str, args: argparse.Namespace) -> float:
    if cid == "C1":
        return (args.launches + 1) * args.browser_timeout + 30.0
    return 2 * args.browser_timeout + 30.0  # C5: two launches + drains


def _browser_subprocess(cid: str, args: argparse.Namespace) -> Criterion:
    """Run a browser criterion in a killable child with a hard wall-clock timeout.

    A stalled Camoufox/Playwright pipe read cannot be interrupted in-process, so
    the child is SIGKILLed on timeout and the criterion is reported SKIPPED.
    The child's stdout/stderr go to temp FILES (not pipes): its Camoufox
    grandchildren inherit the descriptors, so a captured pipe would never reach
    EOF even after the child is killed.
    """
    import tempfile

    budget = _browser_budget(cid, args)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out_fd, out_name = tempfile.mkstemp(prefix=f"ct_{cid}_", suffix=".out", dir=str(SCRATCH))
    err_fd, err_name = tempfile.mkstemp(prefix=f"ct_{cid}_", suffix=".err", dir=str(SCRATCH))
    os.close(out_fd)
    os.close(err_fd)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_browser-child",
        cid,
        "--with-browser",
        "--launches",
        str(args.launches),
        "--browser-timeout",
        str(args.browser_timeout),
    ]
    with open(out_name, "w", encoding="utf-8") as fo, open(err_name, "w", encoding="utf-8") as fe:
        proc = subprocess.Popen(cmd, stdout=fo, stderr=fe)
        try:
            returncode = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            subprocess.run(["pkill", "-f", "camoufox-bin"], capture_output=True, check=False)
            return Criterion(
                cid,
                cid,
                TARGETS.get(cid, ""),
                f"child killed after {budget:.0f}s",
                SKIPPED,
                notes=(
                    "browser phase exceeded its budget: repeated Camoufox launch/terminate "
                    "cycles do not complete in this environment (a single launch is verified "
                    "working). The acceptance run (100 launches / real recycle) needs a stable "
                    "display + browser."
                ),
            )
    stdout = Path(out_name).read_text(encoding="utf-8", errors="replace")
    stderr = Path(err_name).read_text(encoding="utf-8", errors="replace")
    for line in stdout.splitlines():
        if line.startswith(_CHILD_MARKER):
            payload = json.loads(line[len(_CHILD_MARKER) :])
            return Criterion(
                cid,
                payload.get("name", cid),
                payload.get("target", TARGETS.get(cid, "")),
                payload.get("measured", ""),
                payload.get("status", SKIPPED),
                notes=payload.get("notes", ""),
                detail=payload.get("detail", {}),
            )
    return Criterion(
        cid,
        cid,
        TARGETS.get(cid, ""),
        f"child produced no result (rc={returncode})",
        SKIPPED,
        notes=f"browser child failed to report: {stderr[-300:]}",
    )


def main(argv: list[str] | None = None) -> int:
    args = _resolve_args(argv)

    # Hidden child mode: run exactly one browser criterion and emit its JSON.
    if args._browser_child:
        crit = _CRITERIA_BY_ID[args._browser_child](args, {"profiles_dir": _profiles_dir()})
        print(
            _CHILD_MARKER
            + json.dumps(
                {
                    "id": crit.cid,
                    "name": crit.name,
                    "target": crit.target,
                    "measured": crit.measured,
                    "status": crit.status,
                    "notes": crit.notes,
                    "detail": crit.detail,
                },
                default=str,
            )
        )
        return 0

    SCRATCH.mkdir(parents=True, exist_ok=True)
    ctx: dict[str, Any] = {"profiles_dir": _profiles_dir()}

    criteria: list[Criterion] = []
    for _cid, fn in CRITERIA:
        started = time.monotonic()
        try:
            if args.with_browser and _cid in ("C1", "C5"):
                crit = _browser_subprocess(_cid, args)
            else:
                crit = fn(args, ctx)
        except Exception as exc:  # noqa: BLE001
            crit = Criterion(
                _cid,
                _cid,
                TARGETS.get(_cid, ""),
                "unhandled error",
                FAIL,
                notes=f"{type(exc).__name__}: {exc}",
            )
        crit.detail["duration_s"] = round(time.monotonic() - started, 2)
        criteria.append(crit)

    _print_table(criteria)
    print()
    print(
        f"mode: quick={args.quick} with_browser={args.with_browser} "
        f"launches={args.launches} count={args.count} "
        f"sessions={args.sessions} tasks={args.tasks} mutants={args.mutants}"
    )
    print(f"peak RSS (harness process): {_peak_rss_mb():.0f} MB | python {sys.version.split()[0]}")
    for c in criteria:
        if c.status in (SKIPPED, SIMULATED) or c.status == FAIL:
            print(f"  {c.cid} {c.status}: {c.notes}")

    summary = {
        "PASS": sum(1 for c in criteria if c.status == PASS),
        "FAIL": sum(1 for c in criteria if c.status == FAIL),
        "SKIPPED": sum(1 for c in criteria if c.status == SKIPPED),
        "SIMULATED": sum(1 for c in criteria if c.status == SIMULATED),
    }
    report = {
        "schema_version": 1,
        "harness": "benchmarks/harness.py",
        "targets": TARGETS,
        "mode": {
            "quick": args.quick,
            "with_browser": args.with_browser,
            "launches": args.launches,
            "count": args.count,
            "sessions": args.sessions,
            "tasks": args.tasks,
            "mutants": args.mutants,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "peak_rss_mb": round(_peak_rss_mb(), 1),
        },
        "criteria": [
            {
                "id": c.cid,
                "name": c.name,
                "target": c.target,
                "measured": c.measured,
                "status": c.status,
                "notes": c.notes,
                "detail": c.detail,
            }
            for c in criteria
        ],
        "summary": summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\nreport: {args.output}")
    print(f"summary: {summary}")
    return 0 if summary["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
