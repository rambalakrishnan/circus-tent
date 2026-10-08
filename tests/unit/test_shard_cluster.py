"""Unit tests for shard lifecycle + cluster routing/recycling (fake browser).

No real browser: ``circus_tent.browser.shard._launch`` is monkeypatched to
return a fake async context manager whose ``__aenter__`` yields a fake
persistent context; ``capture_fingerprint`` is faked so no page script runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from circus_tent.browser import cluster_manager as cm_mod
from circus_tent.browser import shard as shard_mod
from circus_tent.browser.cluster_manager import ClusterManager
from circus_tent.browser.shard import Shard, ShardState
from circus_tent.config.loader import BackoffConfig, ConfigLoader, ShardConfig
from circus_tent.telemetry import get_metrics

BACKOFF = BackoffConfig(
    triggers=(429, 403, 503),
    initial_wait_seconds=0.01,
    max_wait_seconds=0.02,
    cooldown_seconds=0.01,
)


# --------------------------------------------------------------------- fakes


class FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []
        self.closed = 0

    async def goto(self, url: str, **kwargs: object) -> None:
        self.goto_calls.append(url)

    async def evaluate(self, script: str) -> str:
        return "{}"

    async def close(self) -> None:
        self.closed += 1


class FakeContext:
    """Stands in for a Playwright persistent BrowserContext."""

    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.close_calls = 0

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.close_calls += 1


class FakeCM:
    """Fake Camoufox async launch context manager."""

    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self.exited = 0

    async def __aenter__(self) -> FakeContext:
        return self._context

    async def __aexit__(self, *exc: object) -> bool:
        self.exited += 1
        return False


# ------------------------------------------------------------------ helpers


def install_browser(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list, list]:
    """Monkeypatch the launch seam; return (launched_options, contexts, cms)."""
    launched: list[dict] = []
    contexts: list[FakeContext] = []
    cms: list[FakeCM] = []

    def fake_launch(options: dict) -> FakeCM:
        launched.append(options)
        context = FakeContext()
        cm = FakeCM(context)
        contexts.append(context)
        cms.append(cm)
        return cm

    monkeypatch.setattr(shard_mod, "_launch", fake_launch)
    return launched, contexts, cms


def install_fingerprint(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake capture_fingerprint; returns a mutable state holder."""
    state = {"fp": {"canvas": "aaa", "webgl": "bbb"}}

    async def fake_capture(page: object) -> dict:
        return dict(state["fp"])

    monkeypatch.setattr(shard_mod, "capture_fingerprint", fake_capture)
    return state


def shard_config(
    name: str,
    patterns: tuple[str, ...],
    profile_dir: str,
    *,
    max_tabs: int = 3,
    headless: str = "virtual",
) -> ShardConfig:
    return ShardConfig(
        name=name,
        domain_patterns=patterns,
        profile_dir=profile_dir,
        max_tabs=max_tabs,
        recycle_after_pages=500,
        recycle_grace_seconds=1.0,
        headless=headless,
        request_rate_per_minute=6000.0,
        session_step_rate_per_minute=6000.0,
        backoff=BACKOFF,
        preflight_url="",
        preflight_marker="",
    )


def make_shard(
    tmp_path: Path, name: str = "workday", *, max_tabs: int = 3, headless: str = "virtual"
) -> Shard:
    cfg = shard_config(
        name, (f"*.{name}.test",), f"profiles/{name}", max_tabs=max_tabs, headless=headless
    )
    return Shard(cfg, tmp_path / "profiles" / name, get_metrics(), logging.getLogger("test.shard"))


class FakeLoader:
    def __init__(self, configs: tuple[ShardConfig, ...], config_dir: Path) -> None:
        self._configs = configs
        self.config_dir = config_dir
        self._real = ConfigLoader(config_dir)

    def load_shards(self) -> tuple[ShardConfig, ...]:
        return self._configs

    def shard_for_domain(self, domain: str, shards: tuple[ShardConfig, ...]) -> ShardConfig:
        return self._real.shard_for_domain(domain, shards)


def make_cluster(tmp_path: Path, configs: tuple[ShardConfig, ...]) -> ClusterManager:
    loader = FakeLoader(configs, tmp_path / "config")
    return ClusterManager(loader, get_metrics(), logging.getLogger("test.cluster"))


def two_shard_configs() -> tuple[ShardConfig, ...]:
    return (
        shard_config("workday", ("*.workday.test",), "profiles/workday"),
        shard_config("misc", ("*",), "profiles/misc"),
    )


# ------------------------------------------------------------- shard lifecycle


async def test_initialize_creates_profile_and_sets_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched, _contexts, _cms = install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    shard = make_shard(tmp_path)

    await shard.initialize()

    assert shard.state == ShardState.READY
    profile = tmp_path / "profiles" / "workday"
    assert profile.is_dir()
    assert (profile / "fingerprint.json").is_file()
    assert (profile / "user_data").is_dir()
    # launched with the manifest options, which pin the persistent context
    assert launched[0]["persistent_context"] is True
    assert launched[0]["user_data_dir"] == str(profile / "user_data")


async def test_resolve_manifest_immutable_through_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched, _contexts, _cms = install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    manifest_path = tmp_path / "profiles" / "workday" / "fingerprint.json"

    first = make_shard(tmp_path, headless="virtual")
    await first.initialize()
    hash1 = json.loads(manifest_path.read_text())["manifest_hash"]

    # A second shard on the SAME profile dir with a different headless config:
    # the existing manifest must win (immutability).
    second = make_shard(tmp_path, headless="false")
    await second.initialize()
    hash2 = json.loads(manifest_path.read_text())["manifest_hash"]

    assert hash1 == hash2
    assert launched[1]["headless"] == "virtual"  # manifest value, not the new config
    assert launched[1]["fingerprint"] == launched[0]["fingerprint"]


async def test_health_check_matches_snapshot_and_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_browser(monkeypatch)
    state = install_fingerprint(monkeypatch)
    shard = make_shard(tmp_path)
    await shard.initialize()

    assert await shard.health_check() is True
    assert shard.snapshot().fingerprint_ok is True

    state["fp"] = {"canvas": "different"}  # simulate fingerprint drift
    assert await shard.health_check() is False
    snapshot = shard.snapshot()
    assert snapshot.fingerprint_ok is False
    assert "drift" in (snapshot.last_error or "")


async def test_semaphore_cap_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    shard = make_shard(tmp_path, max_tabs=2)
    await shard.initialize()

    held = [await shard.acquire() for _ in range(2)]
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(shard.acquire(), timeout=0.1)  # blocked at cap

    await shard.release(held[0])
    extra = await asyncio.wait_for(shard.acquire(), timeout=1.0)  # freed permit

    await shard.release(extra)
    await shard.release(held[1])


async def test_drain_returns_after_inflight_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    shard = make_shard(tmp_path)
    await shard.initialize()

    async def work() -> int:
        await asyncio.sleep(0.05)
        return 7

    task = asyncio.create_task(work())
    shard.track(task)

    await asyncio.wait_for(shard.drain(2.0), timeout=3.0)

    assert task.done() and task.result() == 7
    assert shard.state == ShardState.DRAINING


async def test_terminate_closes_browser_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launched, contexts, cms = install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    shard = make_shard(tmp_path)
    await shard.initialize()
    context, cm = contexts[0], cms[0]

    await shard.terminate()
    assert shard.state == ShardState.TERMINATED
    assert context.close_calls == 1
    assert cm.exited == 1

    await shard.terminate()  # idempotent: nothing to close again
    assert context.close_calls == 1
    assert cm.exited == 1


# --------------------------------------------------------------- cluster


async def test_cluster_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    cluster = make_cluster(tmp_path, two_shard_configs())
    await cluster.start()

    assert cluster.shard_for_domain("acme.workday.test").name == "workday"
    assert cluster.shard_for_domain("unmatched.example").name == "misc"


async def test_run_tab_acquires_releases_and_increments_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launched, contexts, _cms = install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    cluster = make_cluster(tmp_path, two_shard_configs())
    await cluster.start()

    async def task(tc: object) -> int:
        assert getattr(tc, "page") is not None
        return 123

    result = await cluster.run_tab("acme.workday.test", "sess", task)

    assert result == 123
    shard = cluster.shard_for_domain("acme.workday.test")
    assert shard.state == ShardState.READY
    assert shard.snapshot().pages_processed == 1  # incremented on completion, no cooldown
    assert contexts[0].close_calls == 0  # context object stays open, page is closed
    assert contexts[0].pages[0].closed == 1  # the tab page was released


async def test_recycle_orders_lifecycle_and_swaps_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    events: list[tuple[int, str]] = []

    class RecordingShard(Shard):
        async def initialize(self, headless_override: str | None = None) -> None:
            events.append((id(self), "initialize"))
            await super().initialize(headless_override)

        async def drain(self, grace_seconds: float) -> None:
            events.append((id(self), "drain"))
            await super().drain(grace_seconds)

        async def flush(self) -> None:
            events.append((id(self), "flush"))
            await super().flush()

        async def terminate(self) -> None:
            events.append((id(self), "terminate"))
            await super().terminate()

        async def health_check(self) -> bool:
            events.append((id(self), "health_check"))
            return await super().health_check()

    monkeypatch.setattr(cm_mod, "Shard", RecordingShard)
    install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    cluster = make_cluster(tmp_path, two_shard_configs())
    await cluster.start()

    old = cluster.shard_for_domain("acme.workday.test")
    await old.initialize()
    old.increment_pages(7)

    replacement = await cluster.recycle("workday")

    assert replacement is not old
    assert cluster.shard_for_domain("acme.workday.test") is replacement

    old_names = [name for ident, name in events if ident == id(old)]
    assert old_names.index("drain") < old_names.index("flush") < old_names.index("terminate")

    new_names = [name for ident, name in events if ident == id(replacement)]
    assert new_names[:2] == ["initialize", "health_check"]
    assert any(record.getMessage() == "shard_recycled" for record in caplog.records)


async def test_shutdown_clean_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    cluster = make_cluster(tmp_path, two_shard_configs())
    await cluster.start()
    await cluster.shard_for_domain("acme.workday.test").initialize()

    assert await cluster.shutdown(0.05) == 0


async def test_shutdown_forced_returns_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutShard(Shard):
        async def drain(self, grace_seconds: float) -> None:
            raise TimeoutError("drain timed out")

    monkeypatch.setattr(cm_mod, "Shard", TimeoutShard)
    install_browser(monkeypatch)
    install_fingerprint(monkeypatch)
    cluster = make_cluster(tmp_path, two_shard_configs())
    await cluster.start()

    assert await cluster.shutdown(0.05) == 1


# ----------------------------------------------------------- pid resolution


def test_pid_falls_back_to_proc_scan_for_persistent_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent contexts expose no process handle (context.browser is None).

    Regression guard for the recycle-safety property: terminate() waits for the
    old browser to exit before a replacement may reuse the profile dir. If the
    PID resolves to None that wait is silently skipped, which is exactly the
    corrupted-profile failure the recycle design exists to prevent.
    """
    from circus_tent.browser import shard as shard_mod

    shard = make_shard(tmp_path)
    shard._browser = SimpleNamespace(browser=None)  # persistent-context shape

    monkeypatch.setattr(shard_mod, "_find_browser_pid", lambda profile_dir: 4321, raising=False)
    monkeypatch.setattr(shard_mod, "_pid_alive", lambda pid: True)

    assert shard._pid() == 4321
    assert shard._pid() == 4321  # cached while alive


def test_pid_is_none_without_a_browser(tmp_path: Path) -> None:
    shard = make_shard(tmp_path)
    assert shard._pid() is None


def test_pid_alive_probe_reports_reaped_pids() -> None:
    from circus_tent.browser.shard import _pid_alive

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(2**22 + 12345) is False  # outside pid_max: reaped
