#!/usr/bin/env python3
"""Pruned-fixture generator (tests/fixtures/README.md, amendment D2).

Reads every ``tests/fixtures/captured_raw/*.html`` and runs the parser module's
:func:`circus_tent.parser.trimmer.prune_html` in BOTH profiles (EXTRACT and HEAL),
writing the deterministic pruned outputs next to the committed fixtures:

    tests/fixtures/<name>.pruned.extract.txt
    tests/fixtures/<name>.pruned.heal.txt

and a manifest describing each source:

    tests/fixtures/MANIFEST.json

The generator is idempotent and deterministic: identical inputs always produce
byte-identical outputs (``prune_html`` is pure, output order is sorted, and no
timestamps are embedded). It performs no network access and calls no model.

Usage:
    python3 benchmarks/make_fixtures.py [--raw-dir DIR] [--out-dir DIR] [--check]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from circus_tent.parser.trimmer import prune_html

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RAW = REPO / "tests" / "fixtures" / "captured_raw"
DEFAULT_OUT = REPO / "tests" / "fixtures"

#: Pruning-profile ceilings from ARCHITECTURE §5.4 (EXTRACT ≤250KB, HEAL ≤50KB).
EXTRACT_LIMIT_BYTES = 250 * 1024
HEAL_LIMIT_BYTES = 50 * 1024

MANIFEST = "MANIFEST.json"


def _parse_meta(path: Path) -> dict[str, str]:
    """Parse a ``key: value`` meta file into a dict (missing file -> empty)."""
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


def _ratio(pruned_bytes: int, raw_bytes: int) -> float:
    if raw_bytes <= 0:
        return 0.0
    return round(1.0 - (pruned_bytes / raw_bytes), 6)


def _prune_both(html: str) -> tuple[str, str]:
    """Run prune_html in BOTH profiles; each call spins its own loop so repeated
    invocation is clean and the two profiles never share parser state."""

    async def _run() -> tuple[str, str]:
        extract = await prune_html(html, "EXTRACT")
        heal = await prune_html(html, "HEAL")
        return extract, heal

    return asyncio.run(_run())


def build_manifest(raw_dir: Path, out_dir: Path, *, check: bool = False) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for html_path in sorted(raw_dir.glob("*.html")):
        name = html_path.stem
        raw_bytes = html_path.stat().st_size
        raw_text = html_path.read_text(encoding="utf-8", errors="replace")
        extract, heal = _prune_both(raw_text)

        extract_path = out_dir / f"{name}.pruned.extract.txt"
        heal_path = out_dir / f"{name}.pruned.heal.txt"
        extract_bytes = len(extract.encode("utf-8"))
        heal_bytes = len(heal.encode("utf-8"))

        if check:
            stale = []
            if not extract_path.is_file() or extract_path.read_text(encoding="utf-8") != extract:
                stale.append(extract_path.name)
            if not heal_path.is_file() or heal_path.read_text(encoding="utf-8") != heal:
                stale.append(heal_path.name)
        else:
            stale = []
            extract_path.write_text(extract, encoding="utf-8", newline="\n")
            heal_path.write_text(heal, encoding="utf-8", newline="\n")

        meta = _parse_meta(raw_dir / f"{name}.meta.txt")
        sources.append(
            {
                "source": name,
                "source_file": f"captured_raw/{html_path.name}",
                "url": meta.get("url", ""),
                "captured_at": meta.get("captured_at", ""),
                "http_status": meta.get("status", ""),
                "raw_bytes": raw_bytes,
                "extract_bytes": extract_bytes,
                "heal_bytes": heal_bytes,
                "reduction_ratio": _ratio(extract_bytes, raw_bytes),
                "heal_reduction_ratio": _ratio(heal_bytes, raw_bytes),
                "extract_oversize": extract_bytes > EXTRACT_LIMIT_BYTES,
                "heal_oversize": heal_bytes > HEAL_LIMIT_BYTES,
                "stale_outputs": stale,
            }
        )

    total_raw = sum(s["raw_bytes"] for s in sources)
    total_extract = sum(s["extract_bytes"] for s in sources)
    total_heal = sum(s["heal_bytes"] for s in sources)
    return {
        "schema_version": 1,
        "generated_by": "benchmarks/make_fixtures.py",
        "generator": "circus_tent.parser.trimmer.prune_html",
        "profiles": {
            "EXTRACT": {"limit_bytes": EXTRACT_LIMIT_BYTES},
            "HEAL": {"limit_bytes": HEAL_LIMIT_BYTES},
        },
        "totals": {
            "sources": len(sources),
            "raw_bytes": total_raw,
            "extract_bytes": total_extract,
            "heal_bytes": total_heal,
            "reduction_ratio": _ratio(total_extract, total_raw),
            "heal_reduction_ratio": _ratio(total_heal, total_raw),
        },
        "sources": sources,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pruned fixtures + MANIFEST.json")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if any pruned fixture is stale/missing",
    )
    args = parser.parse_args()

    raw_dir: Path = args.raw_dir
    out_dir: Path = args.out_dir
    if not raw_dir.is_dir():
        print(f"raw dir not found: {raw_dir}")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(raw_dir, out_dir, check=args.check)
    manifest_path = out_dir / MANIFEST
    if not args.check:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    for src in manifest["sources"]:
        state = "STALE" if src["stale_outputs"] else "ok"
        print(
            f"{src['source']:18} raw={src['raw_bytes']:>8}B "
            f"extract={src['extract_bytes']:>7}B ({src['reduction_ratio']:.1%}) "
            f"heal={src['heal_bytes']:>7}B ({src['heal_reduction_ratio']:.1%}) {state}"
        )
    totals = manifest["totals"]
    print(
        f"{'TOTAL':18} raw={totals['raw_bytes']:>8}B "
        f"extract={totals['extract_bytes']:>7}B ({totals['reduction_ratio']:.1%}) "
        f"heal={totals['heal_bytes']:>7}B ({totals['heal_reduction_ratio']:.1%})"
    )
    if args.check:
        stale = [s["source"] for s in manifest["sources"] if s["stale_outputs"]]
        print(f"check: {'STALE ' + ', '.join(stale) if stale else 'all outputs current'}")
        return 1 if stale else 0
    print(f"wrote {manifest['totals']['sources']} pruned fixture pairs + {manifest_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
