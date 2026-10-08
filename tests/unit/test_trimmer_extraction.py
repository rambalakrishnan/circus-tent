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
