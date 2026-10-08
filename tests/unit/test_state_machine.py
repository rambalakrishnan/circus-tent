"""Unit tests for the execution engine (Learn-Run-Heal tier machine).

No browser, no network: the cluster, page, and model client are fakes; only
the documented public API of the engine and its collaborators is exercised.
"""

from __future__ import annotations

import json
import logging
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from circus_tent.config.loader import BudgetsConfig
from circus_tent.engine.budgets import BudgetManager
from circus_tent.engine.coordinate_cache import compute_dhash
from circus_tent.engine.ledger import ExecutionLedger
from circus_tent.engine.models import HealResponse, VisionResponse
from circus_tent.engine.state_machine import (
    CoordinateCacheStore,
    ExecutionEngine,
    IdempotencyConflict,
    SelectorCacheStore,
)
from circus_tent.telemetry import get_metrics

DOMAIN = "workdayjobs.com"
DOMAIN_KEY = "workdayjobs"
SHARD_NAME = "workday"
ACCOUNT = "acct"


# --------------------------------------------------------------------- fakes


class InteractionError(Exception):
    """Raised by the fake locator for selectors that must not resolve."""


class FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:  # noqa: F821  (forward ref)
        self.page = page
        self.selector = selector

    def _guard(self) -> None:
        if self.selector in self.page.fail_selectors:
            raise InteractionError(f"selector not resolvable: {self.selector}")

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self._guard()
        self.page.filled.append((self.selector, value))

    async def click(self, timeout: int | None = None) -> None:
        self._guard()
        self.page.clicked.append(self.selector)

    async def select_option(self, label: str | None = None, timeout: int | None = None) -> None:
        self._guard()
        self.page.selected.append((self.selector, label))

    async def bounding_box(self, timeout: int | None = None) -> dict[str, float]:
        self._guard()
        return dict(self.page.box)


class FakePage:
    def __init__(
        self,
        body_text: str = "",
        png: bytes | None = None,
        fail_selectors: frozenset[str] = frozenset(),
        box: dict[str, float] | None = None,
        viewport: tuple[int, int] = (1440, 900),
    ) -> None:
        self.body_text = body_text
        self.png = png if png is not None else _png((10, 20, 30))
        self.fail_selectors = fail_selectors
        self.box = box or {"x": 10.0, "y": 10.0, "width": 50.0, "height": 20.0}
        self.viewport = viewport
        self.goto_calls: list[str] = []
        self.wait_calls: list[int] = []
        self.evaluate_calls: list[str] = []
        self.locator_calls: list[str] = []
        self.set_input_files_calls: list[tuple[str, str]] = []
        self.screenshot_calls: list[Any] = []
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.selected: list[tuple[str, str | None]] = []
        self.mouse = FakeMouse()
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.goto_calls.append(url)
        return SimpleNamespace(status=200)

    async def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)

    async def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        if "innerWidth" in script:
            return list(self.viewport)
        if "document.body" in script:
            return self.body_text
        return ""

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        return FakeLocator(self, selector)

    async def set_input_files(self, selector: str, path: str, timeout: int | None = None) -> None:
        self.set_input_files_calls.append((selector, path))

    async def screenshot(self, clip: Any = None, type: str | None = None) -> bytes:
        self.screenshot_calls.append(clip)
        return self.png

    def frames(self) -> list[Any]:
        return []

    async def close(self) -> None:
        self.closed = True


def _png(color: tuple[int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeShard:
    def __init__(self, name: str = SHARD_NAME) -> None:
        self.name = name
        self.config = SimpleNamespace(preflight_url="", preflight_marker="")


class FakeCluster:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.responses: list[tuple[str, int, bool]] = []

    def shard_for_domain(self, domain: str) -> FakeShard:
        return FakeShard()

    async def run_tab(self, domain: str, session_key: str | None, task: Any) -> Any:
        tc = SimpleNamespace(page=self.page, context=None, issued_at=0.0, session_key=session_key)
        return await task(tc)

    def report_response(self, domain: str, status: int, is_challenge: bool) -> None:
        self.responses.append((domain, status, is_challenge))


class FakeModel:
    def __init__(
        self,
        heal_resp: HealResponse | None = None,
        vision_resp: VisionResponse | None = None,
    ) -> None:
        self.heal_resp = heal_resp
        self.vision_resp = vision_resp
        self.heal_calls: list[tuple[str, str, str]] = []
        self.locate_calls: list[tuple[str, str]] = []
        self.context: tuple[str | None, str] | None = None

    def set_run_context(self, session_key: str | None, shard: str) -> None:
        self.context = (session_key, shard)

    async def heal(self, pruned_dom: str, fallback_text: str, failed_selector: str) -> HealResponse:
        self.heal_calls.append((pruned_dom, fallback_text, failed_selector))
        if self.heal_resp is None:
            raise AssertionError("heal called but no response configured")
        return self.heal_resp

    async def locate(self, image_b64: str, fallback_text: str) -> VisionResponse:
        self.locate_calls.append((image_b64, fallback_text))
        assert self.vision_resp is not None
        return self.vision_resp


# ------------------------------------------------------------------ helpers


def _budget_manager() -> BudgetManager:
    cfg = BudgetsConfig(
        session_daily_text_tokens=100_000,
        session_daily_vision_tokens=100_000,
        shard_daily_text_tokens=100_000,
        shard_daily_vision_tokens=100_000,
        cb_window_seconds=3600.0,
        cb_heal_rate_threshold=0.3,
        cb_cooldown_seconds=600.0,
    )
    return BudgetManager(cfg, get_metrics(), logging.getLogger("test.budgets"))


def _seed_selector(
    caches: SelectorCacheStore,
    step_id: str,
    selector: str,
    strategy: str = "css",
    confidence: float = 0.9,
) -> None:
    caches.cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "domain": DOMAIN,
        "version": "2026.10",
        "prompt_version": 1,
        "last_verified": "",
        "steps": {
            step_id: {
                "selector": selector,
                "strategy": strategy,
                "confidence": confidence,
                "hit_count": 0,
                "heal_count": 0,
                "stale": False,
                "fallback_text": "",
                "last_verified": "",
            }
        },
    }
    (caches.cache_dir / f"{DOMAIN_KEY}.json").write_text(json.dumps(payload), encoding="utf-8")


def _engine(
    tmp_path: Path,
    page: FakePage,
    model: FakeModel,
    *,
    budgets: BudgetManager | None = None,
    ledger: ExecutionLedger | None = None,
    seed: tuple[str, str] | None = None,
) -> tuple[ExecutionEngine, FakeCluster]:
    caches = SelectorCacheStore(tmp_path / "selectors", 1)
    if seed is not None:
        _seed_selector(caches, seed[0], seed[1])
    engine = ExecutionEngine(
        FakeCluster(page),
        caches,
        CoordinateCacheStore(tmp_path / "coordinates"),
        model,
        ledger or ExecutionLedger(tmp_path / "ledger"),
        budgets or _budget_manager(),
        None,
        get_metrics(),
        logging.getLogger("test.engine"),
        upload_root=tmp_path / "uploads",
    )
    return engine, engine.cluster  # type: ignore[return-value]


def _step(step_id: str, step_type: str, **fields: Any) -> dict[str, Any]:
    return {"id": step_id, "type": step_type, **fields}


async def _run(
    engine: ExecutionEngine,
    key: str,
    steps: list[dict[str, Any]],
    domain: str = DOMAIN,
) -> Any:
    return await engine.run(
        {"idempotency_key": key, "domain": domain, "account": ACCOUNT, "steps": steps}
    )


# --------------------------------------------------------------- tier ladder


async def test_tier1_cached_selector_success(tmp_path: Path) -> None:
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel(), seed=("s1", "#email"))
    result = await _run(engine, "k1", [_step("s1", "fill", selector_ref="s1", value="a@b.com")])

    assert result.status == "completed"
    step = result.steps[0]
    assert (step.status, step.tier, step.selector) == ("ok", 1, "#email")
    assert page.filled == [("#email", "a@b.com")]
    # cache hit recorded on success
    assert engine.selector_cache_for(DOMAIN).entries()["s1"].hit_count == 1


async def test_tier2_heal_writes_cache_with_decremented_confidence(tmp_path: Path) -> None:
    page = FakePage(fail_selectors=frozenset({"#old"}))
    model = FakeModel(heal_resp=HealResponse("#new", "css", 0.8, "recovered"))
    engine, _ = _engine(tmp_path, page, model, seed=("s1", "#old"))
    result = await _run(
        engine, "k2", [_step("s1", "fill", selector_ref="s1", value="x", fallback_text="email")]
    )

    step = result.steps[0]
    assert (step.status, step.tier, step.selector) == ("healed", 2, "#new")
    assert page.filled == [("#new", "x")]
    # heal invoked with pruned DOM + fallback_text + failed selector
    assert model.heal_calls[0][1:] == ("email", "#old")
    entry = engine.selector_cache_for(DOMAIN).entries()["s1"]
    assert entry.selector == "#new"
    assert entry.confidence == pytest.approx(0.75)  # 0.8 - CONFIDENCE_DECAY(0.05)
    assert entry.heal_count == 1


async def test_tier2_skipped_when_circuit_open(tmp_path: Path) -> None:
    page = FakePage(fail_selectors=frozenset({"#old"}))
    model = FakeModel(vision_resp=VisionResponse(None, None, None, None, 0.0))
    budgets = _budget_manager()
    # Drive the breaker open: heal rate above the 0.3 threshold.
    for _ in range(4):
        budgets.record_step_outcome(SHARD_NAME, True)
    assert budgets.circuit_open(SHARD_NAME) is True

    engine, _ = _engine(tmp_path, page, model, budgets=budgets)
    result = await _run(engine, "k3", [_step("s1", "click", selector="#old", fallback_text="go")])

    # Heal tier never ran; vision tier was attempted instead.
    assert model.heal_calls == []
    assert model.locate_calls != []
    assert result.steps[0].status == "failed"
    assert "vision model could not locate" in (result.steps[0].error or "")


async def test_tier3_vision_clicks_box_center_and_caches(tmp_path: Path) -> None:
    png = _png((200, 40, 60))
    page = FakePage(fail_selectors=frozenset({"#old"}), png=png)
    model = FakeModel(
        heal_resp=HealResponse(None, None, 0.0, "no idea"),
        vision_resp=VisionResponse(100, 200, 40, 20, 0.9),
    )
    engine, _ = _engine(tmp_path, page, model, seed=("s1", "#old"))
    result = await _run(engine, "k4", [_step("s1", "click", selector_ref="s1")])

    step = result.steps[0]
    assert (step.status, step.tier) == ("vision", 3)
    # heal returned an unusable selector, so vision located a box and clicked its center
    assert model.locate_calls != []
    assert page.mouse.clicks == [(120.0, 210.0)]  # x + w/2, y + h/2
    entry = engine.coordinate_cache_for(DOMAIN).get(compute_dhash(png), (1440, 900))
    assert entry is not None
    assert (entry.x, entry.y, entry.width, entry.height, entry.step_id) == (100, 200, 40, 20, "s1")
    assert (tmp_path / "coordinates" / f"{DOMAIN_KEY}.json").is_file()


# ------------------------------------------------------------------- uploads


async def test_upload_missing_file_fails(tmp_path: Path) -> None:
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel())
    result = await _run(engine, "u1", [_step("u1", "upload", file_ref="missing.pdf")])
    assert result.steps[0].status == "failed"
    assert "not found" in (result.steps[0].error or "")


async def test_upload_oversize_fails(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "big.pdf").write_bytes(b"x" * 100)
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel())
    result = await _run(engine, "u2", [_step("u2", "upload", file_ref="big.pdf", max_bytes=10)])
    assert result.steps[0].status == "failed"
    assert "exceeds max_bytes" in (result.steps[0].error or "")


async def test_upload_mime_not_in_allowlist_fails(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "doc.txt").write_text("hello", encoding="utf-8")
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel())
    result = await _run(
        engine,
        "u3",
        [_step("u3", "upload", file_ref="doc.txt", mime_types=("application/pdf",))],
    )
    assert result.steps[0].status == "failed"
    assert "not in allowlist" in (result.steps[0].error or "")


async def test_upload_direct_uses_set_input_files(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "resume.pdf").write_bytes(b"%PDF-1.4 test")
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel())
    result = await _run(
        engine,
        "u4",
        [
            _step(
                "u4",
                "upload",
                file_ref="resume.pdf",
                selector="#file",
                mode="direct",
                mime_types=("application/pdf",),
            )
        ],
    )
    assert result.steps[0].status == "ok"
    assert page.set_input_files_calls == [("#file", str(uploads / "resume.pdf"))]


async def test_upload_dnd_dispatches_without_set_input_files(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "resume.pdf").write_bytes(b"%PDF-1.4 test")
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel())
    result = await _run(
        engine,
        "u5",
        [
            _step(
                "u5",
                "upload",
                file_ref="resume.pdf",
                selector="#dropzone",
                mode="dnd",
                mime_types=("application/pdf",),
            )
        ],
    )
    assert result.steps[0].status == "ok"
    assert page.set_input_files_calls == []
    assert any("DataTransfer" in script for script in page.evaluate_calls)


# ------------------------------------------------------------- assert/checkpoint


async def test_assert_step_pass_and_fail(tmp_path: Path) -> None:
    page = FakePage(body_text="Your application was Submitted successfully")
    engine, _ = _engine(tmp_path, page, FakeModel())
    ok = await _run(engine, "a1", [_step("a1", "assert", text="Submitted")])
    assert ok.steps[0].status == "ok"

    page2 = FakePage(body_text="nothing to see")
    engine2, _ = _engine(tmp_path / "second", page2, FakeModel())
    bad = await _run(engine2, "a2", [_step("a2", "assert", text="Submitted")])
    assert bad.steps[0].status == "failed"
    assert "assert failed" in (bad.steps[0].error or "")


async def test_checkpoint_writes_to_ledger(tmp_path: Path) -> None:
    page = FakePage()
    ledger = ExecutionLedger(tmp_path / "ledger")
    engine, _ = _engine(tmp_path, page, FakeModel(), ledger=ledger)
    result = await _run(engine, "c1", [_step("cp", "checkpoint", note="after page 3")])

    assert result.steps[0].status == "ok"
    assert result.steps[0].tier is None
    run = ledger.get_by_key("c1")
    assert run is not None
    assert [(c.step_id, c.index, c.note) for c in run.checkpoints] == [("cp", 0, "after page 3")]


# ---------------------------------------------------------------- idempotency


async def test_idempotent_replay_returns_stored_statuses(tmp_path: Path) -> None:
    page = FakePage()
    ledger = ExecutionLedger(tmp_path / "ledger")
    engine, _ = _engine(tmp_path, page, FakeModel(), ledger=ledger)
    steps = [_step("s1", "wait", milliseconds=1)]

    first = await _run(engine, "idem", steps)
    assert first.status == "completed"
    assert page.wait_calls == [1]

    replay = await _run(engine, "idem", steps)
    assert replay.status == "completed"
    assert replay.steps[0].status == "ok"  # stored status, not re-executed
    assert page.wait_calls == [1]  # no second execution

    with pytest.raises(IdempotencyConflict):
        await _run(engine, "idem", [_step("s2", "wait", milliseconds=1)])


async def test_side_effecting_step_already_completed_is_not_replayed(tmp_path: Path) -> None:
    page = FakePage()
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run-x", "resume-1", ACCOUNT, SHARD_NAME, DOMAIN, ["cp", "submit", "tail"])
    ledger.record_checkpoint("run-x", "cp", 0, "")
    ledger.mark_side_effect_done("run-x", "submit")

    engine, _ = _engine(tmp_path, page, FakeModel(), ledger=ledger)
    result = await _run(
        engine,
        "resume-1",
        [
            _step("cp", "checkpoint"),
            _step("submit", "click", selector="#go", side_effecting=True),
            _step("tail", "wait", milliseconds=1),
        ],
    )

    assert result.status == "resumed"
    submit = result.steps[1]
    assert submit.status == "ok"
    assert "already completed" in (submit.error or "")
    assert page.clicked == []  # the side-effecting click was never replayed


async def test_steps_before_last_checkpoint_are_skipped(tmp_path: Path) -> None:
    page = FakePage()
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run-y", "resume-2", ACCOUNT, SHARD_NAME, DOMAIN, ["a", "cp", "b"])
    ledger.record_checkpoint("run-y", "cp", 1, "")

    engine, _ = _engine(tmp_path, page, FakeModel(), ledger=ledger)
    result = await _run(
        engine,
        "resume-2",
        [
            _step("a", "wait", milliseconds=1),
            _step("cp", "checkpoint"),
            _step("b", "wait", milliseconds=1),
        ],
    )

    assert result.status == "resumed"
    assert result.steps[0].status == "skipped"
    assert page.wait_calls == [1]  # only the post-checkpoint step ran


# ------------------------------------------------- extract step payload wiring


async def test_extract_step_surfaces_payload_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: StepResult is a FROZEN dataclass, so the extraction
    payload must be a declared field. An earlier implementation attached it
    after construction (`result._payload = ...`) and raised FrozenInstanceError,
    failing every extract step — this test pins the corrected wiring."""
    from circus_tent.engine import state_machine as sm
    from circus_tent.parser.extraction import ExtractionResult
    from circus_tent.parser.trimmer import StabilizationResult

    captured: dict[str, Any] = {}

    async def fake_extract_page(
        page: Any,
        query: str | None = None,
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ExtractionResult:
        captured["query"] = query
        captured["schema"] = schema
        return ExtractionResult(
            markdown="# Title\nSalary: 100000\n",
            structured={"salary": 100000},
            schema_incomplete=True,
            stabilization=StabilizationResult(False, True, 123, ("h1",)),
        )

    monkeypatch.setattr(sm, "extract_page", fake_extract_page)
    page = FakePage()
    engine, _ = _engine(tmp_path, page, FakeModel())
    result = await _run(
        engine,
        "k-extract",
        [_step("s1", "extract", query="salary", schema={"type": "object"})],
    )

    step = result.steps[0]
    assert step.status == "ok"
    assert captured == {"query": "salary", "schema": {"type": "object"}}
    assert result.extracted == {"salary": 100000}
    assert result.schema_incomplete is True
    assert result.stabilization_partial is True
    assert step.payload is not None
    assert step.payload["markdown"].startswith("# Title")


async def test_extract_step_without_markdown_fails_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty extraction is a step failure, never a crashed run."""
    from circus_tent.engine import state_machine as sm
    from circus_tent.parser.extraction import ExtractionResult

    async def empty_extract_page(page: Any, **kwargs: Any) -> ExtractionResult:
        return ExtractionResult(
            markdown="", structured=None, schema_incomplete=False, stabilization=None
        )

    monkeypatch.setattr(sm, "extract_page", empty_extract_page)
    engine, _ = _engine(tmp_path, FakePage(), FakeModel())
    result = await _run(engine, "k-extract-empty", [_step("s1", "extract")])

    assert result.status == "failed"
    assert result.steps[0].status == "failed"
    assert "empty markdown" in (result.steps[0].error or "")
