"""Unit tests for the parser: prune_html profiles and deterministic extraction."""

from __future__ import annotations

import asyncio

from circus_tent.parser.extraction import schema_guided_extract
from circus_tent.parser.trimmer import prune_html

SAMPLE = """
<html><head><style>.x{}</style><script>var a=1;</script><meta name="csrf" content="tok"></head>
<body>
  <h1>Job Posting</h1>
  <div class="layout" aria-label="main-panel">
    <p>Senior Engineer role</p>
    <img src="logo.png" alt="Acme Corp">
    <svg aria-label="calendar"><path/></svg>
    <button role="submit">Apply Now</button>
    <noscript>please enable js</noscript>
    <iframe src="https://tracker.example.com"></iframe>
    <label for="email">Email</label>
    <input id="email" type="text" aria-label="Email Address">
  </div>
</body></html>
"""


def test_extract_profile_keeps_structure_and_markers() -> None:
    out = asyncio.run(prune_html(SAMPLE, "EXTRACT"))
    assert "Job Posting" in out
    assert "[ICON:Acme Corp]" in out
    assert "[ICON:calendar]" in out
    assert "[BUTTON:submit:Apply Now]" in out
    assert "Email Address" in out  # aria-label preserved
    assert "tok" not in out  # meta stripped
    assert "var a=1" not in out  # script stripped
    assert "please enable js" not in out  # noscript stripped
    assert "tracker.example.com" not in out  # iframe stripped
    assert "<div" in out  # structure retained in EXTRACT


def test_heal_profile_drops_layout_wrappers() -> None:
    out = asyncio.run(prune_html(SAMPLE, "HEAL"))
    assert "<div" not in out  # layout wrapper unwrapped
    assert "Senior Engineer role" in out  # text retained
    assert "<input" in out  # interactive retained
    assert "[BUTTON:submit:Apply Now]" in out
    assert "<p" not in out  # non-interactive wrapper dropped


def test_whitespace_collapse_and_empty_dom() -> None:
    out = asyncio.run(prune_html("<div>  a   b \n\n c </div>", "EXTRACT"))
    assert "a b c" in out
    assert asyncio.run(prune_html("", "EXTRACT")) == ""


def test_schema_guided_extract_deterministic() -> None:
    md = """# Job
    Title: Senior Engineer
    Location: Remote
    Salary: 150000
    Skills:
    - python
    - kubernetes
    """
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "location": {"type": "string"},
            "salary": {"type": "integer"},
        },
    }
    result, incomplete = schema_guided_extract(md, schema)
    assert not incomplete
    assert result == {"title": "Senior Engineer", "location": "Remote", "salary": 150000}


def test_schema_guided_extract_partial_on_missing_fields() -> None:
    md = "Title: Only Title\n"
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "location": {"type": "string"},
        },
        "required": ["title", "location"],
    }
    result, incomplete = schema_guided_extract(md, schema)
    assert incomplete
    assert result == {"title": "Only Title"}


# --------------------------------------------- regression: void-element swallowing


def test_pruner_does_not_swallow_document_after_void_elements() -> None:
    """Regression: void elements (<meta>, <link>) have no closing tag.

    A depth-counter implementation never returned to zero after the first void
    tag and silently dropped the rest of the document — a 5KB LinkedIn authwall
    pruned to 13 bytes. The pruner must track one skipped tag, not a depth.
    """
    html = (
        "<html><head>"
        "<meta charset='utf-8'><meta name='viewport' content='w=1'><link rel='x' href='y'>"
        "<title>T</title></head><body>"
        "<h1>Visible Heading</h1><p>Visible body text that must survive pruning.</p>"
        "<script>var hidden = 'DROP ME';</script>"
        "<meta name='another'>"
        "<p>Text after a second void element.</p>"
        "</body></html>"
    )
    out = asyncio.run(prune_html(html, "EXTRACT"))
    assert "Visible Heading" in out
    assert "Visible body text that must survive pruning." in out
    assert "Text after a second void element." in out
    assert "DROP ME" not in out


def test_pruner_keeps_text_on_captured_fixtures() -> None:
    """Guard against the real-world version of the bug above: a large page must
    not prune down to a near-empty stub. Skips when raw captures are absent
    (they are gitignored)."""
    import pathlib

    raw_dir = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "captured_raw"
    expectations = {
        "linkedin.html": "LinkedIn",
        "governmentjobs.html": "GovernmentJobs",
        "greenhouse.html": "Opportunities",
    }
    checked = 0
    for filename, needle in expectations.items():
        path = raw_dir / filename
        if not path.exists():
            continue
        raw = path.read_text(errors="replace")
        out = asyncio.run(prune_html(raw, "EXTRACT"))
        assert len(out) > 500, f"{filename} pruned to {len(out)} bytes"
        assert needle in out, f"{filename}: expected marker {needle!r} in pruned output"
        checked += 1
    if checked == 0:
        import pytest

        pytest.skip("no raw captures present (gitignored)")
