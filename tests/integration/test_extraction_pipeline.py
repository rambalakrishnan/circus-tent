"""Integration tests for the in-process Crawl4AI extraction pipeline.

Exercises the real ``circus_tent.parser.extraction`` pipeline against the
committed captured fixtures. The browser launch step is stubbed (no browser
binary / no network in CI): Crawl4AI's ``raw:`` fast path returns the supplied
HTML directly, and the genuine pruning + markdown-generation code still runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from circus_tent.parser.extraction import extract_html, schema_guided_extract
from circus_tent.parser.trimmer import prune_html

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "captured_raw"

# Two content-rich pages are driven through Crawl4AI; the 700KB lever capture is
# only pruned (per the fixture-generation guidance) to keep runtime modest.
CRAWL_PAGES = ("greenhouse", "workday_login")
ALL_PAGES = (
    "greenhouse",
    "lever",
    "governmentjobs",
    "linkedin",
    "indeed",
    "workday_login",
    "workday_shell",
)

#: EXTRACT profile target ceiling (ARCHITECTURE §5.4: EXTRACT ≤ 250KB).
EXTRACT_PROFILE_LIMIT = 250_000

SAMPLE_MARKDOWN = """
# Job
Title: Senior Engineer
Location: Remote
Salary: 150000
Skills:
- python
- kubernetes
"""


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the browser launch/teardown; keep the real markdown pipeline."""
    from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy

    async def _noop(self: object) -> None:
        return None

    monkeypatch.setattr(AsyncPlaywrightCrawlerStrategy, "start", _noop)
    monkeypatch.setattr(AsyncPlaywrightCrawlerStrategy, "close", _noop)


def _read_fixture(name: str) -> str | None:
    path = FIXTURE_DIR / f"{name}.html"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


async def test_extract_html_produces_markdown(no_browser: None) -> None:
    crawled = 0
    for name in CRAWL_PAGES:
        html = _read_fixture(name)
        if html is None:
            continue
        crawled += 1
        markdown, structured, schema_incomplete = await extract_html(html)
        assert isinstance(markdown, str)
        assert markdown.strip() != ""  # larger pages yield real markdown
        assert structured is None
        assert schema_incomplete is False

    if crawled == 0:
        pytest.skip("captured fixtures are absent")


def test_schema_guided_extract_is_deterministic() -> None:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "location": {"type": "string"},
            "salary": {"type": "integer"},
        },
    }
    first, first_incomplete = schema_guided_extract(SAMPLE_MARKDOWN, schema)
    second, second_incomplete = schema_guided_extract(SAMPLE_MARKDOWN, schema)

    assert first == second
    assert first_incomplete is False and second_incomplete is False
    assert first == {"title": "Senior Engineer", "location": "Remote", "salary": 150000}


def test_invalid_or_unsatisfiable_schema_returns_incomplete() -> None:
    # Non-object schema: unsatisfiable, flagged, never raised.
    result, incomplete = schema_guided_extract(SAMPLE_MARKDOWN, {"type": "array"})
    assert result is None
    assert incomplete is True

    # Structurally valid but unsatisfiable (required field absent): best-effort.
    partial_schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "location": {"type": "string"}},
        "required": ["title", "location"],
    }
    partial, partial_incomplete = schema_guided_extract("Title: Only Title\n", partial_schema)
    assert partial_incomplete is True
    assert partial is None or isinstance(partial, dict)


async def test_extract_profile_prune_stays_under_size_bound() -> None:
    checked = 0
    for name in ALL_PAGES:
        html = _read_fixture(name)
        if html is None:
            continue
        checked += 1
        pruned = await prune_html(html, "EXTRACT")
        assert isinstance(pruned, str)
        assert len(pruned) <= EXTRACT_PROFILE_LIMIT

    if checked == 0:
        pytest.skip("captured fixtures are absent")
