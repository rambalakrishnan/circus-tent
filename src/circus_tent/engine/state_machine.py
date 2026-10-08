"""Learn-Run-Heal state machine + run orchestration. See spec.

Executes typed steps through three tiers:
  Tier 1 Run — cached selectors, zero tokens.
  Tier 2 Heal — pruned DOM + text model (skipped when circuit open).
  Tier 3 Vision — localized crop + perceptual-hash coordinate cache.
Preflight (session validity) gates every run; the execution ledger provides
idempotency resume and at-most-once semantics for side-effecting steps.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal

from circus_tent.browser.cluster_manager import ClusterManager
from circus_tent.browser.rate_limiter import RateLimited
from circus_tent.engine.budgets import BudgetManager
from circus_tent.engine.coordinate_cache import CoordinateCache, CoordinateEntry, compute_dhash
from circus_tent.engine.ledger import ExecutionLedger
from circus_tent.engine.models import HealResponse, ModelClient
from circus_tent.engine.preflight import check_session
from circus_tent.engine.selector_cache import SelectorCache, SelectorEntry
from circus_tent.parser.extraction import ExtractionResult, extract_page
from circus_tent.parser.trimmer import prune_page
from circus_tent.telemetry import Metrics

StepType = Literal[
    "navigate", "fill", "click", "select", "wait", "extract", "assert", "upload", "checkpoint"
]

STEP_TIMEOUT_MS = 10_000
VISION_PADDING_PX = 100


@dataclass(frozen=True)
class Step:
    id: str
    type: StepType
    url: str | None = None
    value: str | None = None
    selector_ref: str | None = None
    selector: str | None = None
    strategy: str = "css"
    fallback_text: str = ""
    side_effecting: bool = False
    milliseconds: int | None = None
    text: str | None = None
    query: str | None = None
    schema: dict[str, Any] | None = None
    file_ref: str | None = None
    mode: str = "direct"
    mime_types: tuple[str, ...] = ()
    max_bytes: int = 5_242_880
    note: str = ""


@dataclass(frozen=True)
class StepResult:
    id: str
    status: str
    tier: int | None
    selector: str | None
    error: str | None
    duration_ms: int
    # Additive field: carries extraction output for `extract` steps so the run
    # loop can surface it on RunResult.extracted. Frozen dataclasses cannot be
    # monkey-patched, so this is a declared field (default None for other steps).
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    idempotency_key: str
    status: str
    resumed_from: int | None
    steps: tuple[StepResult, ...]
    extracted: Any | None
    schema_incomplete: bool
    stabilization_partial: bool
    session: str


class IdempotencyConflict(Exception):
    """Same idempotency_key, different step list, terminal state."""


class SelectorCacheStore:
    """Dict-like facade for per-domain selector caches."""

    def __init__(self, cache_dir: Path, prompt_version: int) -> None:
        self.cache_dir = cache_dir
        self.prompt_version = prompt_version
        self._caches: dict[str, SelectorCache] = {}

    def for_domain(self, domain: str) -> SelectorCache:
        key = domain.split(".")[0] if domain else "misc"
        cache = self._caches.get(key)
        if cache is None:
            cache = SelectorCache(self.cache_dir / f"{key}.json", domain, self.prompt_version)
            cache.load()
            self._caches[key] = cache
        return cache


class CoordinateCacheStore:
    """Dict-like facade for per-domain coordinate caches."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._caches: dict[str, CoordinateCache] = {}

    def for_domain(self, domain: str) -> CoordinateCache:
        key = domain.split(".")[0] if domain else "misc"
        cache = self._caches.get(key)
        if cache is None:
            cache = CoordinateCache(self.cache_dir / f"{key}.json", domain)
            cache.load()
            self._caches[key] = cache
        return cache


class ExecutionEngine:
    def __init__(
        self,
        cluster: ClusterManager,
        caches: SelectorCacheStore,
        coord_caches: CoordinateCacheStore,
        model_client: ModelClient,
        ledger: ExecutionLedger,
        budgets: BudgetManager,
        challenge_resolver: Any | None,
        metrics: Metrics,
        logger: logging.Logger,
        upload_root: Path | None = None,
    ) -> None:
        self.cluster = cluster
        self.caches = caches
        self.coord_caches = coord_caches
        self.model_client = model_client
        self.ledger = ledger
        self.budgets = budgets
        self.challenge_resolver = challenge_resolver
        self.metrics = metrics
        self.logger = logger
        self.upload_root = upload_root or Path.cwd()

    def selector_cache_for(self, domain: str) -> SelectorCache:
        return self.caches.for_domain(domain)

    def coordinate_cache_for(self, domain: str) -> CoordinateCache:
        return self.coord_caches.for_domain(domain)

    # ------------------------------------------------------------------ run

    async def run(self, req: Any) -> RunResult:
        steps = (
            [Step(**dict(s)) for s in req.steps]
            if hasattr(req, "steps")
            else [Step(**s) for s in req["steps"]]
        )
        domain = req.domain if hasattr(req, "domain") else req["domain"]
        account = (
            (req.account or "default")
            if hasattr(req, "account")
            else (req.get("account") or "default")
        )
        idem = req.idempotency_key if hasattr(req, "idempotency_key") else req["idempotency_key"]
        shard = self.cluster.shard_for_domain(domain)
        self.model_client.set_run_context(account, shard.name)

        step_ids = [s.id for s in steps]
        existing = self.ledger.get_by_key(idem)
        if existing is not None and existing.status == "completed":
            if list(existing.step_states.keys()) != step_ids:
                raise IdempotencyConflict(f"key {idem!r} completed with a different step list")
            return RunResult(
                run_id=existing.run_id,
                idempotency_key=idem,
                status="completed",
                resumed_from=None,
                steps=tuple(
                    StepResult(id, existing.step_states.get(id, "skipped"), None, None, None, 0)
                    for id in step_ids
                ),
                extracted=None,
                schema_incomplete=False,
                stabilization_partial=False,
                session="ok",
            )

        run_id = str(uuid.uuid4())
        if existing is None:
            ledger_run = self.ledger.create(run_id, idem, account, shard.name, domain, step_ids)
            resume_index: int | None = None
        else:
            ledger_run = existing
            run_id = existing.run_id
            last = existing.checkpoints[-1] if existing.checkpoints else None
            resume_index = last.index if last else None

        # --- preflight (B3): fail fast on SESSION_EXPIRED
        session = "ok"
        cfg = shard.config
        if cfg.preflight_url and cfg.preflight_marker:
            try:
                ok, reason = await self.cluster.run_tab(
                    domain,
                    account,
                    lambda tc: check_session(tc.page, cfg.preflight_url, cfg.preflight_marker),
                )
                if not ok:
                    session = reason
                    self.logger.warning(
                        "session preflight failed",
                        extra={
                            "event": "session_expired"
                            if reason == "SESSION_EXPIRED"
                            else "preflight_error",
                            "shard": shard.name,
                            "reason": reason,
                        },
                    )
                    self.ledger.finalize(run_id, "failed")
                    return self._run_result(
                        run_id, idem, "failed", resume_index, (), None, False, False, session
                    )
            except RateLimited:
                return self._run_result(
                    run_id,
                    idem,
                    "failed",
                    resume_index,
                    (),
                    None,
                    False,
                    False,
                    "PREFLIGHT_ERROR",
                )
        elif resume_index is not None:
            session = "ok"

        results: list[StepResult] = []
        extracted: Any = None
        schema_incomplete = False
        stabilization_partial = False
        failed = False

        for idx, step in enumerate(steps):
            if resume_index is not None and idx < resume_index:
                # steps before the checkpoint are skipped on resume — EXCEPT
                # completed side-effecting steps which stay ok (at-most-once).
                if step.id in ledger_run.side_effect_completed:
                    results.append(StepResult(step.id, "ok", None, None, None, 0))
                else:
                    results.append(StepResult(step.id, "skipped", None, None, None, 0))
                continue
            if step.side_effecting and step.id in ledger_run.side_effect_completed:
                results.append(
                    StepResult(step.id, "ok", None, None, "already completed (at-most-once)", 0)
                )
                continue

            result = await self.cluster.run_tab(
                domain,
                account,
                partial(self._exec_step, step=step, domain=domain, run_id=run_id, index=idx),
            )
            results.append(result)
            self.ledger.record_step(run_id, step.id, result.status)
            if result.status in ("ok", "healed", "vision") and step.side_effecting:
                self.ledger.mark_side_effect_done(run_id, step.id)
            if step.type == "extract" and result.status == "ok":
                payload = result.payload
                if payload:
                    extracted = payload.get("structured")
                    schema_incomplete = payload.get("schema_incomplete", False)
                    stabilization_partial = payload.get("stabilization_partial", False)
            if result.status == "failed":
                failed = True

        status = "failed" if failed else ("completed" if resume_index is None else "resumed")
        self.ledger.finalize(run_id, status)
        return self._run_result(
            run_id,
            idem,
            status,
            resume_index,
            tuple(results),
            extracted,
            schema_incomplete,
            stabilization_partial,
            session,
        )

    def _run_result(
        self,
        run_id: str,
        idem: str,
        status: str,
        resumed: int | None,
        steps: tuple[StepResult, ...],
        extracted: Any,
        schema_incomplete: bool,
        stabilization_partial: bool,
        session: str,
    ) -> RunResult:
        return RunResult(
            run_id=run_id,
            idempotency_key=idem,
            status=status,
            resumed_from=resumed,
            steps=steps,
            extracted=extracted,
            schema_incomplete=schema_incomplete,
            stabilization_partial=stabilization_partial,
            session=session,
        )

    async def extract_url(
        self, domain: str, url: str, query: str | None, schema: dict[str, Any] | None
    ) -> ExtractionResult:
        async def task(tc: Any) -> ExtractionResult:
            await tc.page.goto(url, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
            return await extract_page(tc.page, query=query, schema=schema)

        return await self.cluster.run_tab(domain, None, task)

    # ------------------------------------------------------------ step exec

    async def _exec_step(
        self, tc: Any, step: Step, domain: str, run_id: str, index: int
    ) -> StepResult:
        started = time.monotonic()

        def result(
            status: str,
            tier: int | None,
            selector: str | None,
            error: str | None,
            payload: dict[str, Any] | None = None,
        ) -> StepResult:
            return StepResult(
                step.id,
                status,
                tier,
                selector,
                error,
                int((time.monotonic() - started) * 1000),
                payload,
            )

        page = tc.page
        try:
            if step.type == "navigate":
                return await self._navigate(page, step, domain)
            if step.type == "wait":
                await page.wait_for_timeout(step.milliseconds or 0)
                return result("ok", 1, None, None)
            if step.type == "checkpoint":
                self.ledger.record_checkpoint(run_id, step.id, index, step.note)
                return result("ok", None, None, None)
            if step.type == "assert":
                ok, err = await self._assert(page, step)
                return result("ok" if ok else "failed", 1, None, err)
            if step.type == "extract":
                payload = await self._extract(page, step)
                return result(
                    "ok" if payload["markdown"] else "failed",
                    1,
                    None,
                    None if payload["markdown"] else "extraction returned empty markdown",
                    payload,
                )
            if step.type == "upload":
                return await self._upload(page, step)
            # interactive steps (fill/click/select): tier ladder
            return await self._interactive(tc, step, domain)
        except Exception as e:  # noqa: BLE001
            return result("failed", None, None, f"{type(e).__name__}: {e}")

    async def _navigate(self, page: Any, step: Step, domain: str) -> StepResult:
        response = await page.goto(step.url, wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
        status = getattr(response, "status", 200) if response else 200
        is_challenge = False
        if status >= 400 and self.challenge_resolver is not None:
            try:
                pruned = await prune_page(page, "HEAL")
                is_challenge = await self.challenge_resolver.handle(page, pruned, page.url, domain)
            except Exception:  # noqa: BLE001
                is_challenge = False
        self.cluster.report_response(domain, status, is_challenge)
        if status >= 400 and not is_challenge:
            return StepResult(step.id, "failed", 1, None, f"HTTP {status}", self._elapsed())
        return StepResult(step.id, "ok", 1, None, None, self._elapsed())

    def _elapsed(self) -> int:
        return 0  # placeholder; duration is computed by _exec_step's wrapper

    async def _assert(self, page: Any, step: Step) -> tuple[bool, str | None]:
        try:
            text = await page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:  # noqa: BLE001
            text = ""
        needle = (step.text or "").lower()
        if needle in text.lower():
            return True, None
        snippet = text[:200]
        return False, f"assert failed: {step.text!r} not in page text (…{snippet}…)"

    async def _extract(self, page: Any, step: Step) -> dict[str, Any]:
        result = await extract_page(page, query=step.query, schema=step.schema)
        return {
            "markdown": result.markdown,
            "structured": result.structured,
            "schema_incomplete": result.schema_incomplete,
            "stabilization_partial": bool(result.stabilization and result.stabilization.partial),
        }

    async def _upload(self, page: Any, step: Step) -> StepResult:
        file_ref = step.file_ref or ""
        path = Path(file_ref)
        if not path.is_absolute():
            path = self.upload_root / path
        if not path.is_file():
            return StepResult(step.id, "failed", 1, None, f"upload file not found: {file_ref}", 0)
        size = path.stat().st_size
        if size > step.max_bytes:
            return StepResult(
                step.id,
                "failed",
                1,
                None,
                f"upload size {size} exceeds max_bytes {step.max_bytes}",
                0,
            )
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if step.mime_types and mime not in step.mime_types:
            return StepResult(
                step.id,
                "failed",
                1,
                None,
                f"upload mime {mime} not in allowlist {step.mime_types}",
                0,
            )
        selector = step.selector or step.selector_ref or ""
        if step.mode == "dnd":
            await self._dnd_upload(page, selector, path, mime)
        else:
            await page.set_input_files(selector, str(path), timeout=STEP_TIMEOUT_MS)
        return StepResult(step.id, "ok", 1, selector, None, 0)

    async def _dnd_upload(self, page: Any, selector: str, path: Path, mime: str) -> None:
        import json as _json

        data = base64.b64encode(path.read_bytes()).decode()
        js = f"""async () => {{
          const zone = document.querySelector({_json.dumps(selector)});
          if (!zone) throw new Error('drop zone not found: {selector}');
          const bin = atob({_json.dumps(data)});
          const arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          const file = new File([arr], {_json.dumps(path.name)}, {{type: {_json.dumps(mime)}}});
          const dt = new DataTransfer();
          dt.items.add(file);
          for (const ev of ['dragenter', 'dragover', 'drop']) {{
            const opts = {{bubbles: true, cancelable: true, dataTransfer: dt}};
            zone.dispatchEvent(new DragEvent(ev, opts));
          }}
        }}"""
        await page.evaluate(js)

    # ------------------------------------------------------------ tier ladder

    def _resolve_selector(self, step: Step, domain: str) -> tuple[str | None, str, str | None]:
        """(selector, strategy, cache_key) — cache entry or inline selector."""
        cache_key = step.selector_ref or step.id
        if step.selector:
            return step.selector, step.strategy, None
        entry = self.selector_cache_for(domain).get(cache_key)
        if entry is not None:
            return entry.selector, entry.strategy, cache_key
        return None, "css", None

    async def _interactive(self, tc: Any, step: Step, domain: str) -> StepResult:
        page = tc.page

        selector, strategy, cache_key = self._resolve_selector(step, domain)
        # Tier 1
        if selector:
            try:
                await self._native_op(page, step, selector, strategy)
                if cache_key:
                    self.selector_cache_for(domain).record_hit(cache_key)
                return StepResult(step.id, "ok", 1, selector, None, 0)
            except Exception as err:  # noqa: BLE001
                tier1_error = f"{type(err).__name__}: {err}"
        else:
            tier1_error = "no cached or inline selector"

        # Tier 2 — heal
        shard = self.cluster.shard_for_domain(domain)
        heal_error = "heal skipped (circuit open)"
        if not self.budgets.circuit_open(shard.name):
            try:
                pruned = await prune_page(page, "HEAL")
                resp: HealResponse = await self.model_client.heal(
                    pruned, step.fallback_text or step.id, selector or ""
                )
                self.budgets.record_step_outcome(shard.name, True)
                if resp.selector and resp.strategy in ("css", "xpath"):
                    try:
                        await self._native_op(page, step, resp.selector, resp.strategy)
                    except Exception:  # noqa: BLE001
                        self.budgets.record_step_outcome(shard.name, False)
                    else:
                        if cache_key:
                            entry = SelectorEntry(
                                selector=resp.selector,
                                strategy=resp.strategy,
                                confidence=max(resp.confidence, 0.1),
                                fallback_text=step.fallback_text,
                            )
                            self.selector_cache_for(domain).record_heal(cache_key, entry)
                        return StepResult(step.id, "healed", 2, resp.selector, None, 0)
                else:
                    self.budgets.record_step_outcome(shard.name, False)
            except Exception as heal_exc:  # noqa: BLE001
                self.budgets.record_step_outcome(shard.name, False)
                heal_error = f"{type(heal_exc).__name__}: {heal_exc}"

        # Tier 3 — vision (coordinate fallback)
        try:
            ok, selector_used, vision_err = await self._vision(page, step, domain, shard.name)
            return StepResult(
                step.id,
                "vision" if ok else "failed",
                3 if ok else None,
                selector_used,
                vision_err,
                0,
            )
        except Exception as vision_exc:  # noqa: BLE001
            return StepResult(
                step.id,
                "failed",
                None,
                None,
                f"tier1: {tier1_error}; {heal_error}; "
                f"vision: {type(vision_exc).__name__}: {vision_exc}",
                0,
            )

    async def _native_op(self, page: Any, step: Step, selector: str, strategy: str) -> None:
        locator = page.locator(selector) if strategy == "css" else page.locator(f"xpath={selector}")
        if step.type == "fill":
            await locator.fill(step.value or "", timeout=STEP_TIMEOUT_MS)
        elif step.type == "click":
            await locator.click(timeout=STEP_TIMEOUT_MS)
        elif step.type == "select":
            await locator.select_option(label=step.value or "", timeout=STEP_TIMEOUT_MS)

    async def _vision(
        self, page: Any, step: Step, domain: str, shard_name: str
    ) -> tuple[bool, str | None, str | None]:
        selector, strategy, _ = self._resolve_selector(step, domain)
        box: dict[str, float] | None = None
        if selector:
            with contextlib.suppress(Exception):
                locator = (
                    page.locator(selector)
                    if strategy == "css"
                    else page.locator(f"xpath={selector}")
                )
                box = await locator.bounding_box(timeout=3000)
        if box is None:
            with contextlib.suppress(Exception):
                box = await page.locator("body").bounding_box(timeout=3000)
        if box is None:
            box = {"x": 0.0, "y": 0.0, "width": 1440.0, "height": 900.0}

        pad = VISION_PADDING_PX
        clip = {
            "x": max(0, box["x"] - pad),
            "y": max(0, box["y"] - pad),
            "width": box["width"] + 2 * pad,
            "height": box["height"] + 2 * pad,
        }
        png = await page.screenshot(clip=clip, type="png")
        phash = compute_dhash(png)
        viewport = await page.evaluate("[window.innerWidth, window.innerHeight]")
        vp = (int(viewport[0]), int(viewport[1]))

        cache = self.coordinate_cache_for(domain)
        entry = cache.get(phash, vp)
        if entry is not None:
            await page.mouse.click(entry.x + entry.width / 2, entry.y + entry.height / 2)
            entry.hit_count += 1
            cache.save()
            return True, selector, None

        b64 = base64.b64encode(png).decode()
        resp = await self.model_client.locate(b64, step.fallback_text or step.id)
        if resp.x is None or resp.y is None:
            return False, None, "vision model could not locate the element"
        cx = resp.x + (resp.width or 10) / 2
        cy = resp.y + (resp.height or 10) / 2
        await page.mouse.click(cx, cy)
        cache.record(
            phash,
            CoordinateEntry(
                x=int(resp.x),
                y=int(resp.y),
                width=int(resp.width or 10),
                height=int(resp.height or 10),
                viewport=vp,
                step_id=step.id,
            ),
        )
        cache.save()
        return True, selector, None
