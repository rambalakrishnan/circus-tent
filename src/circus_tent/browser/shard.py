"""Single shard lifecycle. See modules/browser/.omp-spec.md.

Tab model note (deviation from the brief's wording, documented): Camoufox
launches ONE persistent context per shard (cookies/storage live in
user_data/). Concurrent tabs are Pages inside that single context — launching
separate contexts per tab would fork the same profile dir and corrupt it.
TabContext therefore carries the shared persistent context plus a fresh Page.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from circus_tent.browser.fingerprint import (
    build_launch_options,
    capture_fingerprint,
    compare_fingerprints,
    fingerprint_matches,
    resolve_manifest,
)
from circus_tent.config.loader import ShardConfig
from circus_tent.telemetry import Metrics


class ShardState(StrEnum):
    INITIALIZING = "initializing"
    READY = "ready"
    DRAINING = "draining"
    TERMINATED = "terminated"


@dataclass
class TabContext:
    context: Any
    page: Any
    issued_at: float
    session_key: str | None = None


@dataclass
class ShardHealth:
    name: str
    state: ShardState
    tabs_active: int
    pages_processed: int
    fingerprint_ok: bool
    pid: int | None
    cooldown_until: float | None
    last_error: str | None


def _launch(launch_options: dict[str, Any]) -> Any:
    """Lazy Camoufox launch factory. Pinned call shape for camoufox 0.5.7:
    ``AsyncCamoufox(**persistent_context_options)`` is an ASYNC CONTEXT MANAGER
    whose ``__aenter__()`` returns the persistent BrowserContext (verified
    against the installed 0.5.7 by introspection). Callers must keep the
    context manager object and pair ``__aenter__`` with ``__aexit__``."""
    from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

    return AsyncCamoufox(**launch_options)  # type: ignore[no-untyped-call]


async def _enter(camoufox_cm: Any) -> Any:
    """Enter the Camoufox async context manager and return the persistent
    BrowserContext."""
    return await camoufox_cm.__aenter__()


def _find_browser_pid(profile_dir: Path) -> int | None:
    """Best-effort PID lookup for the shard's browser process.

    Playwright's persistent-context handle exposes no process object (unlike a
    normal `launch()`, `context.browser` is None), so the process is located by
    scanning /proc for a cmdline that references this profile directory. This
    matters beyond bookkeeping: `terminate()` waits for the old process to exit
    before a replacement may reuse the profile dir — without a real PID that
    wait was silently skipped, risking the corrupted-profile failure the
    recycle design exists to prevent.
    """
    needle = str(profile_dir)
    proc_root = Path("/proc")
    if not proc_root.exists():  # non-Linux: no /proc to scan
        return None
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if needle in cmdline:
            return int(entry.name)
    return None


def _pid_alive(pid: int) -> bool:
    """Liveness probe: signal 0 performs error checking without sending a signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


class Shard:
    """One isolated Camoufox process bound to a domain family + profile dir."""

    def __init__(
        self, config: ShardConfig, profile_dir: Path, metrics: Metrics, logger: logging.Logger
    ) -> None:
        self.config = config
        self.profile_dir = profile_dir
        self.name = config.name
        self.metrics = metrics
        self.logger = logger
        self._state = ShardState.TERMINATED
        self._browser: Any = None
        self._camoufox_cm: Any = None
        self._pid_cache: int | None = None
        self._semaphore = asyncio.Semaphore(config.max_tabs)
        self._pages_processed = 0
        self._init_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._manifest: Any = None
        self._fp_snapshot: dict[str, Any] = {}
        self._fingerprint_ok = True
        self._last_error: str | None = None

    @property
    def state(self) -> ShardState:
        return self._state

    @property
    def needs_recycle(self) -> bool:
        return self._pages_processed >= self.config.recycle_after_pages

    def _pid(self) -> int | None:
        if self._browser is None:
            return None
        proc = getattr(getattr(self._browser, "browser", None), "process", None)
        if proc is not None:
            return int(proc.pid)
        # Persistent contexts expose no process handle — fall back to a /proc
        # scan, cached while the process is still alive so repeated snapshots
        # stay cheap and stable.
        cached = self._pid_cache
        if cached is not None and _pid_alive(cached):
            return cached
        found = _find_browser_pid(self.profile_dir)
        self._pid_cache = found
        return found

    async def initialize(self, headless_override: str | None = None) -> None:
        """INITIALIZING → READY. Concurrent callers await the first init."""
        async with self._init_lock:
            if self._state == ShardState.READY:
                return
            self._state = ShardState.INITIALIZING
            self._last_error = None
            try:
                profile_dir = self.profile_dir
                profile_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(profile_dir, 0o700)
                (profile_dir / "user_data").mkdir(exist_ok=True)
                headless = headless_override or self.config.headless
                launch_options = build_launch_options(profile_dir, headless, humanize=True)
                self._manifest = resolve_manifest(profile_dir, launch_options)
                if not fingerprint_matches(self._manifest, profile_dir):
                    self._fingerprint_ok = False
                # Launch with the MANIFEST options, never the freshly built ones —
                # a manifest from an earlier launch wins (immutability).
                self._camoufox_cm = _launch(dict(self._manifest.launch_options))
                self._browser = await _enter(self._camoufox_cm)
                self._fp_snapshot = await self._capture()
                self._state = ShardState.READY
                self.logger.info(
                    "shard ready",
                    extra={"event": "shard_ready", "shard": self.name, "pid": self._pid()},
                )
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
                self._state = ShardState.TERMINATED
                raise

    async def _capture(self) -> dict[str, Any]:
        page = await self._browser.new_page()
        try:
            await page.goto("about:blank")
            return await capture_fingerprint(page)
        finally:
            await page.close()

    async def health_check(self) -> bool:
        """about:blank responds + fingerprint equals the init snapshot."""
        if self._state != ShardState.READY or self._browser is None:
            return False
        try:
            current = await self._capture()
            differing = compare_fingerprints(current, self._fp_snapshot)
            self._fingerprint_ok = not differing
            if differing:
                self._last_error = f"fingerprint drift: {differing}"
            else:
                self._last_error = None
            return self._fingerprint_ok
        except Exception as e:  # noqa: BLE001
            self._fingerprint_ok = False
            self._last_error = f"{type(e).__name__}: {e}"
            return False

    async def acquire(self, session_key: str | None = None) -> TabContext:
        await self._semaphore.acquire()
        try:
            page = await self._browser.new_page()
            return TabContext(
                context=self._browser,
                page=page,
                issued_at=time.monotonic(),
                session_key=session_key,
            )
        except Exception:
            self._semaphore.release()
            raise

    async def release(self, tc: TabContext) -> None:
        try:
            try:
                await tc.page.close()
            finally:
                self._semaphore.release()
        finally:
            pass

    async def drain(self, grace_seconds: float) -> None:
        """DRAINING: wait for in-flight tab tasks to complete (≤ grace)."""
        self._state = ShardState.DRAINING
        if not self._active_tasks:
            return
        done, pending = await asyncio.wait(list(self._active_tasks), timeout=grace_seconds)
        for task in pending:
            task.cancel()

    async def flush(self) -> None:
        """Explicit storage flush: close the persistent context and tear down
        the Camoufox async context manager (keeps the driver session clean)."""
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._camoufox_cm is not None:
            with contextlib.suppress(Exception):
                await self._camoufox_cm.__aexit__(None, None, None)
            self._camoufox_cm = None

    async def terminate(self) -> None:
        """Close the browser and wait for the process to fully exit (≤30s).

        Firefox writes cookies/storage asynchronously; the replacement shard
        may only relaunch against this profile AFTER the old PID is gone.
        """
        pid = self._pid()
        await self.flush()
        self._state = ShardState.TERMINATED
        if pid is None:
            return
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.2)

    def track(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    def increment_pages(self, n: int = 1) -> None:
        self._pages_processed += n
        self.metrics.pages_processed.labels(shard=self.name).inc(n)

    def persist_health(self) -> None:
        path = self.profile_dir / "health.json"
        data = {
            "state": self._state.value,
            "pages_processed": self._pages_processed,
            "pid": self._pid(),
            "fingerprint_ok": self._fingerprint_ok,
            "last_error": self._last_error,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    def snapshot(self) -> ShardHealth:
        return ShardHealth(
            name=self.name,
            state=self._state,
            tabs_active=len(self._active_tasks),
            pages_processed=self._pages_processed,
            fingerprint_ok=self._fingerprint_ok,
            pid=self._pid(),
            cooldown_until=None,
            last_error=self._last_error,
        )
