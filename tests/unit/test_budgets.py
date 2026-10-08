"""Unit tests for budgets: caps, rollover, circuit breaker."""

from __future__ import annotations

import logging

import pytest

from circus_tent.config.loader import BudgetsConfig
from circus_tent.engine.budgets import BudgetExceeded, BudgetManager
from circus_tent.telemetry import get_metrics

CFG = BudgetsConfig(
    session_daily_text_tokens=1000,
    session_daily_vision_tokens=500,
    shard_daily_text_tokens=5000,
    shard_daily_vision_tokens=2000,
    cb_window_seconds=3600.0,
    cb_heal_rate_threshold=0.30,
    cb_cooldown_seconds=1800.0,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def bm() -> BudgetManager:
    return BudgetManager(CFG, get_metrics(), logging.getLogger("test.budgets"), clock=FakeClock())


def test_session_cap(bm: BudgetManager) -> None:
    bm.check_and_charge("text", "s1", "workday", 800, 200)
    with pytest.raises(BudgetExceeded) as e:
        bm.check_and_charge("text", "s1", "workday", 10, 0)
    assert e.value.scope == "session"
    # a different session is unaffected
    bm.check_and_charge("text", "s2", "workday", 100, 0)


def test_shard_cap(bm: BudgetManager) -> None:
    # 6 sessions x 900 tokens: each under the 1000 session cap,
    # total 5400 crosses the 5000 shard cap on the last one.
    for i in range(5):
        bm.check_and_charge("text", f"s{i}", "greenhouse", 900, 0)
    with pytest.raises(BudgetExceeded) as e:
        bm.check_and_charge("text", "s5", "greenhouse", 900, 0)
    assert e.value.scope == "shard"


def test_vision_budget_is_separate(bm: BudgetManager) -> None:
    bm.check_and_charge("vision", "s1", "workday", 400, 0)
    with pytest.raises(BudgetExceeded):
        bm.check_and_charge("vision", "s1", "workday", 200, 0)
    bm.check_and_charge("text", "s1", "workday", 100, 0)  # text unaffected


def test_circuit_breaker_opens_on_heal_rate(bm: BudgetManager) -> None:
    for _ in range(10):
        bm.record_step_outcome("indeed", True)
        bm.record_step_outcome("indeed", False)
        bm.record_step_outcome("indeed", False)
    assert bm.circuit_open("indeed")
    assert not bm.circuit_open("lever")


def test_snapshot(bm: BudgetManager) -> None:
    bm.check_and_charge("text", "s1", "workday", 100, 0)
    snap = bm.snapshot()
    assert "budgets" in snap and "circuit_breakers" in snap
