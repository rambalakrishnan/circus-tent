"""Standalone acceptance-criterion-1 tool: real-browser fingerprint stability.

Real-browser verification: Camoufox launch + fingerprint stability (short form).

Runs OUTSIDE pytest (manual verification). Launches the pinned profile N times,
captures the in-page fingerprint each time, and asserts zero drift — the core
mechanism behind acceptance criterion 1.

Usage: python3 /path/to/verify_browser.py [--launches N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


async def one_launch(profile_dir: Path, headless: str) -> tuple[dict, dict]:
    from circus_tent.browser.fingerprint import (
        build_launch_options,
        capture_fingerprint,
        resolve_manifest,
    )
    from circus_tent.browser.shard import _enter, _launch

    opts = build_launch_options(profile_dir, headless)
    manifest = resolve_manifest(profile_dir, opts)
    camoufox_cm = _launch(dict(manifest.launch_options))
    browser = await _enter(camoufox_cm)
    try:
        page = await browser.new_page()
        try:
            await page.goto("about:blank")
            fp = await capture_fingerprint(page)
        finally:
            await page.close()
    finally:
        with __import__("contextlib").suppress(Exception):
            await browser.close()
        with __import__("contextlib").suppress(Exception):
            await camoufox_cm.__aexit__(None, None, None)
    return fp, {"manifest_hash": manifest.manifest_hash, "created_at": manifest.created_at}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launches", type=int, default=3)
    parser.add_argument("--headless", default="virtual", choices=["virtual", "true", "false"])
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="ct-verify-"))
    profile_dir = tmp / "profiles" / "misc_session"
    print(f"profile_dir: {profile_dir}")
    print(f"headless mode: {args.headless!r}")
    print(f"DISPLAY={os.environ.get('DISPLAY')!r}")

    from circus_tent.browser.fingerprint import compare_fingerprints

    fingerprints: list[dict] = []
    hashes: set[str] = set()
    for i in range(1, args.launches + 1):
        try:
            fp, meta = await one_launch(profile_dir, args.headless)
        except Exception as e:  # noqa: BLE001
            print(f"launch {i}: FAILED — {type(e).__name__}: {e}")
            print(json.dumps({"result": "LAUNCH_FAILED", "error": str(e)}))
            return 2
        fingerprints.append(fp)
        hashes.add(meta["manifest_hash"])
        print(
            f"launch {i}: ok  canvas={fp.get('canvas')} webgl={fp.get('webgl')} "
            f"audio={fp.get('audio')} fonts={len(fp.get('fonts') or [])} "
            f"ua={str((fp.get('navigator') or {}).get('userAgent'))[:40]!r}"
        )

    drift = []
    for i in range(1, len(fingerprints)):
        diff = compare_fingerprints(fingerprints[0], fingerprints[i])
        if diff:
            drift.append({"launch": i + 1, "differing_keys": diff})

    perm = oct(profile_dir.stat().st_mode & 0o777)
    manifest_ok = (profile_dir / "fingerprint.json").exists()
    print(f"profile dir mode: {perm} (expect 0o700)")
    print(f"manifest present: {manifest_ok}; distinct manifest hashes: {len(hashes)} (expect 1)")
    print(f"fingerprint drift across launches: {drift or 'NONE'}")

    ok = not drift and len(hashes) == 1 and manifest_ok and perm == "0o700"
    print(
        json.dumps(
            {
                "result": "PASS" if ok else "FAIL",
                "launches": args.launches,
                "headless": args.headless,
                "drift": drift,
                "distinct_manifest_hashes": len(hashes),
                "profile_mode": perm,
            }
        )
    )
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
