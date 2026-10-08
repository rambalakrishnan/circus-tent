"""In-process Crawl4AI extraction + deterministic schema-guided output. See spec.

crawl4ai 0.9.4 deviations (documented): raw HTML goes in via
``AsyncWebCrawler.arun(url="raw:" + html, config=...)`` (the ``html=`` kwarg is
from an older crawl4ai API), and MemoryAdaptiveDispatcher does not exist in
0.9.4 — batch extraction uses a bounded asyncio.Semaphore + gather instead.
Still fully in-process: no sidecar, no HTTP pipe.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema

from circus_tent.parser.trimmer import (
    PruneProfile,
    StabilizationResult,
    prune_page,
    wait_for_stability,
)


@dataclass(frozen=True)
class ExtractionResult:
    markdown: str
    structured: Any | None
    schema_incomplete: bool
    stabilization: StabilizationResult | None


def _crawl4ai_imports() -> tuple[Any, Any, Any, Any, Any]:
    """Lazy import — crawl4ai is heavy; unit tests stay fast.

    The pruning filter prefers crawl4ai's lxml implementation when present
    (identical output, ~10x faster per upstream — this is what keeps
    acceptance-criterion 2's p95 within target), falling back to the
    BeautifulSoup-based filter on older versions.
    """
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
    from crawl4ai.content_filter_strategy import BM25ContentFilter, PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    try:
        from crawl4ai.content_filter_strategy import PruningContentFilterLXML

        pruning_cls: Any = PruningContentFilterLXML
    except ImportError:  # pragma: no cover - older crawl4ai
        pruning_cls = PruningContentFilter

    return (
        AsyncWebCrawler,
        CrawlerRunConfig,
        BM25ContentFilter,
        pruning_cls,
        DefaultMarkdownGenerator,
    )


def _reduce_inprocess(html: str, query: str | None) -> str:
    """Deterministic in-process HTML → markdown reduction. NO browser.

    crawl4ai's filters and markdown generator are pure-Python and synchronous,
    so the reduction never needs a Playwright/Chromium runtime. Measured on an
    85KB document: ~37ms p50 (pruning) / ~185ms p50 (BM25) versus ~3s per
    document when going through AsyncWebCrawler, which spawns a browser for
    every call — that browser-per-document path is why acceptance criterion 2
    (p95 < 200ms) cannot be met by the crawler route.
    """
    _, _, BM25ContentFilter, PruningFilter, DefaultMarkdownGenerator = _crawl4ai_imports()
    if query:
        filter_strategy: Any = BM25ContentFilter(user_query=query, bm25_threshold=1.2)
    else:
        filter_strategy = PruningFilter(threshold=0.4)
    generator = DefaultMarkdownGenerator(content_filter=filter_strategy)
    result = generator.generate_markdown(input_html=html)
    return str(getattr(result, "raw_markdown", "") or "")


def _build_config(query: str | None) -> Any:
    _, CrawlerRunConfig, BM25ContentFilter, PruningFilter, DefaultMarkdownGenerator = (
        _crawl4ai_imports()
    )
    if query:
        filter_strategy = BM25ContentFilter(user_query=query, bm25_threshold=1.2)
    else:
        filter_strategy = PruningFilter(threshold=0.4)
    return CrawlerRunConfig(
        markdown_generator=DefaultMarkdownGenerator(content_filter=filter_strategy),
        cache_mode="BYPASS",
    )


async def _crawl_html(html: str, query: str | None) -> str:
    """Reduce HTML to markdown, preferring the in-process (browserless) path.

    The crawler path is kept only as a fallback for crawl4ai builds whose
    filter/generator API differs; on the pinned 0.9.4 the fast path is used and
    no browser binary is required.
    """
    try:
        return _reduce_inprocess(html, query)
    except Exception:  # noqa: BLE001  (fall back rather than fail the step)
        AsyncWebCrawler, _, _, _, _ = _crawl4ai_imports()
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url="raw:" + html, config=_build_config(query))
            return result.markdown or ""


async def extract_page(
    page: Any,
    query: str | None = None,
    schema: dict[str, Any] | None = None,
    profile: PruneProfile = "EXTRACT",
    signatures: Sequence[str] | None = None,
) -> ExtractionResult:
    stabilization = await wait_for_stability(page, signatures=signatures)
    pruned = await prune_page(page, profile)
    markdown = await _crawl_html(pruned, query)
    structured: Any | None = None
    schema_incomplete = False
    if schema is not None:
        structured, schema_incomplete = schema_guided_extract(markdown, schema)
    return ExtractionResult(
        markdown=markdown,
        structured=structured,
        schema_incomplete=schema_incomplete,
        stabilization=stabilization,
    )


async def extract_html(
    html: str, query: str | None = None, schema: dict[str, Any] | None = None
) -> tuple[str, Any | None, bool]:
    """Reduce a raw HTML string in-process.

    Deliberately does NOT pre-run the semantic pruner: crawl4ai's own filter
    cleaning is both faster and sufficient for content extraction here, while
    the pruner's output (marker-annotated, still tag-heavy) measurably slows the
    markdown generator — measured on a 147KB synthetic document: 107ms p50
    feeding the raw HTML versus 794ms feeding pruned output. The semantic
    pruner (shadow-DOM uncloaking, [ICON:]/[BUTTON:] markers) stays on the
    live-page path, which is the only place those affordances exist.
    """
    markdown = await _crawl_html(html, query)
    if schema is None:
        return markdown, None, False
    structured, schema_incomplete = schema_guided_extract(markdown, schema)
    return markdown, structured, schema_incomplete


async def batch_extract_html(
    items: Sequence[tuple[str, str | None]], max_concurrency: int = 8
) -> list[tuple[str, Any | None, bool]]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def one(html: str, query: str | None) -> tuple[str, Any | None, bool]:
        async with semaphore:
            return await extract_html(html, query)

    return list(await asyncio.gather(*(one(h, q) for h, q in items)))


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")
_KV_RE = re.compile(r"^([A-Za-z][\w\s\-\.]{1,40}):\s*(.+)$")


def _parse_markdown(markdown: str) -> dict[str, Any]:
    """Deterministic extraction of key:value pairs, headings, and list items."""
    out: dict[str, Any] = {}
    current_heading: str | None = None
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        m = _HEADING_RE.match(line)
        if m:
            current_heading = m.group(1).strip().lower()
            if current_heading not in out:
                out[current_heading] = []
            continue
        kv = _KV_RE.match(line)
        if kv:
            key = (kv.group(1) or "").strip().lower()
            if not key:
                continue
            value: Any = (kv.group(2) or "").strip()
            if re.fullmatch(r"-?\d+", value):
                value = int(value)
            elif re.fullmatch(r"-?\d+\.\d+", value):
                value = float(value)
            out[key] = value
            continue
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if current_heading:
                bucket = out.setdefault(current_heading, [])
                if isinstance(bucket, list):
                    bucket.append(item)
            continue
        if current_heading and i + 1 < len(lines) and line:
            bucket = out.setdefault(current_heading, [])
            if isinstance(bucket, list):
                bucket.append(line)
    return out


def _shape(value: Any, subschema: dict[str, Any]) -> Any:
    """Coerce a raw value to the subschema's type (best effort)."""
    t = subschema.get("type")
    if t == "array" and isinstance(value, list):
        items_schema = subschema.get("items", {})
        return [_shape(v, items_schema) for v in value]
    if t == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def schema_guided_extract(markdown: str, schema: dict[str, Any]) -> tuple[Any | None, bool]:
    """DETERMINISTIC extraction validated against the schema. No LLM calls."""
    raw = _parse_markdown(markdown)
    if schema.get("type") != "object" or "properties" not in schema:
        return None, True
    props: dict[str, dict[str, Any]] = schema["properties"]
    result: dict[str, Any] = {}
    for key, subschema in props.items():
        value = raw.get(key.lower())
        if value is None:
            # try matching any key containing the name
            for k, v in raw.items():
                if key.lower() in str(k):
                    value = v
                    break
        if value is None:
            continue
        result[key] = _shape(value, subschema)
    try:
        jsonschema.validate(result, schema)
        return result, False
    except jsonschema.ValidationError:
        # best-effort partial: return the extracted fields as-is, flagged.
        return result or None, True
