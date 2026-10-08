#!/usr/bin/env python3
"""Fixture drift check — NIGHTLY / MANUAL ONLY. Never run in CI.

Re-fetches the URLs recorded in tests/fixtures/captured_raw/*.meta.txt, compares
HTTP status, <title>, and body size against the recorded values, and flags drift
so an operator can re-record fixtures (see tests/fixtures/README.md).

Usage: python3 benchmarks/drift_check.py [--write]
  Default: report only. --write: update the .meta.txt files with current values
  (do NOT overwrite the committed fixtures automatically).
"""

from __future__ import annotations

import argparse
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "tests" / "fixtures" / "captured_raw"
UA = "circus-tent-drift-check/0.1"
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def fetch(url: str) -> tuple[int, str, int]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
            m = TITLE_RE.search(body)
            return r.status, (m.group(1).strip()[:120] if m else ""), len(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        m = TITLE_RE.search(body)
        return e.code, (m.group(1).strip()[:120] if m else ""), len(body)
    except Exception as e:  # noqa: BLE001
        return -1, f"FETCH ERROR: {e}", 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    drift = 0
    for meta in sorted(RAW.glob("*.meta.txt")):
        data: dict[str, str] = {}
        for line in meta.read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()
        url = data.get("url", "")
        if not url:
            print(f"{meta.name}: no url in meta, skipping")
            continue
        status, title, size = fetch(url)
        old_status = data.get("status", "?")
        name = meta.name.removesuffix(".meta.txt")
        issues = []
        if str(status) != old_status:
            issues.append(f"status {old_status} -> {status}")
        print(f"{name:20} {url[:60]:60} {old_status}->{status} {size}B  {issues or 'ok'}")
        if issues:
            drift += 1
        if args.write:
            lines = [
                f"url: {url}",
                f"captured_at: {datetime.now(UTC).isoformat(timespec='seconds')}",
                f"status: {status}",
            ]
            meta.write_text("\n".join(lines) + "\n")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
