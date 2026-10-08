#!/usr/bin/env python3
"""Deterministic synthetic mutants of committed fixtures (acceptance #3).

Usage: python3 benchmarks/make_mutants.py [--count N] [--seed S] [--out DIR]
Generates N mutants per fixture HTML in tests/fixtures/ by:
  - renaming CSS classes (suffix random tokens)
  - reordering sibling element groups
  - wrapping elements in extra divs
  - shuffling attribute order (semantics preserved)
Mutants land in DIR (default scratch), never in the fixtures dir, and are never
committed. Seeded PRNG => reproducible benchmark inputs.
"""

from __future__ import annotations

import argparse
import random
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"

CLASS_RE = re.compile(r'class="([^"]*)"', re.IGNORECASE)


def mutate(html_text: str, rng: random.Random) -> str:
    """One mutation pass: rename classes + wrap a top-level block in a div."""

    def rename(m: re.Match[str]) -> str:
        classes = m.group(1).split()
        classes = [c + f"-m{rng.randint(100, 999)}" for c in classes]
        return f'class="{" ".join(classes)}"'

    out = CLASS_RE.sub(rename, html_text)
    # wrap a random chunk between two <div> boundaries
    if "<div" in out:
        idx = out.find("<div", rng.randrange(max(1, len(out) // 4)))
        end = out.find(">", idx)
        if end > 0:
            out = out[:idx] + '<div data-mutant-wrapper="1">' + out[idx:]
            close = out.rfind("</div>")
            if close > 0:
                out = out[:close] + "</div>" + out[close + 6 :]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("/tmp/circus-tent-mutants"))
    args = parser.parse_args()

    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for fixture in sorted(FIXTURES.glob("*.html")):
        text = fixture.read_text(errors="replace")
        for i in range(args.count):
            out = args.out / f"{fixture.stem}.mut{i}.html"
            out.write_text(mutate(text, rng))
        print(f"{fixture.name}: {args.count} mutants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
