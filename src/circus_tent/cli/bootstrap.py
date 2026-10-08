"""Headful first-run MFA bootstrap per shard. See spec.

The ONLY sanctioned path that creates a fingerprint manifest and the ONLY
sanctioned way to change one (--reset purges profile + manifest and requires
the operator to type RESET — a fingerprint change without a cookie purge is a
"new device" signal and risks account lockout on most ATS platforms).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from pathlib import Path

from circus_tent.browser.fingerprint import build_launch_options, resolve_manifest
from circus_tent.browser.shard import _enter, _launch
from circus_tent.config.loader import ConfigLoader


async def bootstrap_shard(
    shard_name: str, config_dir: Path, logger: logging.Logger, reset: bool = False
) -> int:
    loader = ConfigLoader(config_dir)
    shards = loader.load_shards()
    shard_cfg = next((s for s in shards if s.name == shard_name), None)
    if shard_cfg is None:
        logger.error("unknown shard %r; valid: %s", shard_name, [s.name for s in shards])
        return 2

    profile_dir = Path(shard_cfg.profile_dir)
    manifest_path = profile_dir / "fingerprint.json"

    if reset and profile_dir.exists():
        print(
            "\nWARNING: --reset purges the profile directory INCLUDING the pinned\n"
            "fingerprint manifest and all authenticated session state (cookies, MFA).\n"
            "Changing a fingerprint without clearing cookies makes the session appear\n"
            "to move to a NEW DEVICE — most ATS platforms treat this as fraud and may\n"
            "LOCK THE ACCOUNT.\n"
        )
        if input("Type RESET to continue: ").strip() != "RESET":
            print("Aborted.")
            return 0
        shutil.rmtree(profile_dir, ignore_errors=True)
        logger.warning("profile reset", extra={"event": "profile_reset", "shard": shard_name})

    if manifest_path.exists():
        logger.error(
            "fingerprint manifest already exists for shard %r; pass --reset to purge it",
            shard_name,
        )
        return 2

    print(
        f"\nBootstrapping shard {shard_name!r} (headful).\n"
        f"Profile: {profile_dir}\n"
        "A Firefox window will open. Complete MFA and any one-time onboarding\n"
        "layouts. Then return here and confirm.\n"
    )
    # First-run: headless=False (operator must see the window).
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_options = build_launch_options(profile_dir, "false", humanize=True)
    manifest = resolve_manifest(profile_dir, launch_options)
    camoufox_cm = _launch(dict(manifest.launch_options))
    browser = await _enter(camoufox_cm)

    try:
        page = await browser.new_page()
        if shard_cfg.preflight_url:
            try:
                await page.goto(shard_cfg.preflight_url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                logger.warning("preflight url navigation failed; window still open for manual work")
        while True:
            answer = input("Type 'done' and press Enter once MFA/onboarding is complete: ")
            if answer.strip().lower() == "done":
                break
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
        with contextlib.suppress(Exception):
            await camoufox_cm.__aexit__(None, None, None)

    print(
        f"\nBootstrap complete for shard {shard_name!r}.\n"
        f"Fingerprint manifest: {manifest_path}\n"
        f"Manifest hash: {manifest.manifest_hash}\n"
        f"Created at: {manifest.created_at}\n"
        "The manifest is IMMUTABLE. Subsequent runs use headless='virtual'\n"
        "against this pre-authenticated profile. Start the server with:\n"
        "  circus-tent serve\n"
    )
    return 0
