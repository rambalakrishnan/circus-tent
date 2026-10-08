"""Token budgets + heal-rate circuit breaker (B9). See spec.

Implementation notes
--------------------
* Buckets are keyed by ``(scope, budget_type, key, utc_date)`` where ``scope``
  is ``session`` or ``shard``, ``budget_type`` is ``text`` or ``vision``
  (derived from the role name: any role containing ``vision`` maps to the
  vision budget, everything else to text), and ``key`` is the caller-supplied
  session key or shard name. Unknown keys are auto-created on first charge;
  the UTC date is part of the key, so buckets reset implicitly on UTC-day
  rollover (stale-date buckets are dropped from ``snapshot()``).
* ``check_and_charge`` is atomic: both the session and the shard cap are
  checked before either bucket is updated, so a ``BudgetExceeded`` never
  leaves a partial charge behind. Negative token totals (post-call credits)
  are clamped at zero and never raise.
* The circuit breaker keeps a rolling window of ``(monotonic_time, healed)``
  step outcomes per shard. Once the heal rate strictly exceeds
  ``cb_heal_rate_threshold`` the breaker latches open for
  ``cb_cooldown_seconds``; ``circuit_open`` re-evaluates the window only after
  the cooldown expires.
* Metrics: ``circus_tent_tokens_consumed{shard,role,model}`` is incremented on
  every positive charge (the ``model`` label is empty here — the budget API
  does not receive a model name; the conduit logs actual per-model usage), and
  ``circus_tent_circuit_breaker_state{shard}`` tracks breaker transitions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from circus_tent.config.loader import BudgetsConfig
from circus_tent.telemetry import Metrics

_BucketKey = tuple[str, str, str]  # (scope, budget_type, key)
_BucketState = tuple[str, int]  # (utc_date, tokens_used)
_Outcome = tuple[float, bool]  # (monotonic time, healed)
_BucketView = dict[str, str | int]
_BreakerView = dict[str, object]


class BudgetExceeded(Exception):
    def __init__(self, role: str, scope: str) -> None:
        self.role = role
        self.scope = scope
        super().__init__(f"budget exceeded: role={role} scope={scope}")


def _utc_date() -> str:
    """Current UTC calendar date (``YYYY-MM-DD``) — the budget rollover boundary."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


class BudgetManager:
    def __init__(
        self,
        cfg: BudgetsConfig,
        metrics: Metrics,
        logger: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.logger = logger
        self.clock = clock
        self._buckets: dict[_BucketKey, _BucketState] = {}
        self._window: dict[str, list[_Outcome]] = {}
        self._opened_at: dict[str, float] = {}
        self._breaker_open: dict[str, bool] = {}

    @staticmethod
    def _budget_type(role: str) -> str:
        return "vision" if "vision" in role.lower() else "text"

    def _cap(self, scope: str, budget_type: str) -> int:
        return int(getattr(self.cfg, f"{scope}_daily_{budget_type}_tokens"))

    def _ensure_bucket(self, scope: str, budget_type: str, key: str, today: str) -> _BucketState:
        index = (scope, budget_type, key)
        state = self._buckets.get(index)
        if state is None or state[0] != today:
            state = (today, 0)
            self._buckets[index] = state
        return state

    def check_and_charge(
        self, role: str, session_key: str, shard: str, tokens_in: int, tokens_out: int
    ) -> None:
        budget_type = self._budget_type(role)
        total = int(tokens_in) + int(tokens_out)
        today = _utc_date()
        session_cap = self._cap("session", budget_type)
        shard_cap = self._cap("shard", budget_type)

        _, session_used = self._ensure_bucket("session", budget_type, session_key, today)
        _, shard_used = self._ensure_bucket("shard", budget_type, shard, today)

        new_session = max(0, session_used + total)
        new_shard = max(0, shard_used + total)
        if new_session > session_cap:
            raise BudgetExceeded(role, "session")
        if new_shard > shard_cap:
            raise BudgetExceeded(role, "shard")

        self._buckets[("session", budget_type, session_key)] = (today, new_session)
        self._buckets[("shard", budget_type, shard)] = (today, new_shard)
        if total > 0:
            self.metrics.tokens_consumed.labels(shard=shard, role=role, model="").inc(total)

    def record_step_outcome(self, shard: str, healed: bool) -> None:
        now = self.clock()
        window = self._window.setdefault(shard, [])
        window.append((now, bool(healed)))
        self._prune(shard, now)

    def _prune(self, shard: str, now: float) -> None:
        window = self._window.get(shard)
        if not window:
            return
        cutoff = now - float(self.cfg.cb_window_seconds)
        while window and window[0][0] < cutoff:
            window.pop(0)

    def circuit_open(self, shard: str) -> bool:
        now = self.clock()
        opened_at = self._opened_at.get(shard)
        if opened_at is not None:
            if now - opened_at < float(self.cfg.cb_cooldown_seconds):
                self._set_breaker(shard, True)
                return True
            del self._opened_at[shard]  # cooldown expired -> re-evaluate the window

        self._prune(shard, now)
        window = self._window.get(shard, [])
        total = len(window)
        healed = sum(1 for _, was_healed in window if was_healed)
        open_now = total > 0 and healed / total > float(self.cfg.cb_heal_rate_threshold)
        if open_now:
            self._opened_at[shard] = now
        self._set_breaker(shard, open_now)
        return open_now

    def _set_breaker(self, shard: str, open_state: bool) -> None:
        if self._breaker_open.get(shard) != open_state:
            self._breaker_open[shard] = open_state
            self.metrics.circuit_breaker_state.labels(shard=shard).set(1 if open_state else 0)

    def snapshot(self) -> dict[str, Any]:
        today = _utc_date()
        now = self.clock()
        budgets: dict[str, dict[str, dict[str, _BucketView]]] = {
            "session": {"text": {}, "vision": {}},
            "shard": {"text": {}, "vision": {}},
        }
        for (scope, budget_type, key), (date, used) in self._buckets.items():
            if date != today:
                continue
            cap = self._cap(scope, budget_type)
            budgets[scope][budget_type][key] = {
                "date": date,
                "used": used,
                "cap": cap,
                "remaining": max(0, cap - used),
            }

        breakers: dict[str, _BreakerView] = {}
        shards = set(self._window) | set(self._opened_at) | set(self._breaker_open)
        for shard in sorted(shards):
            opened_at = self._opened_at.get(shard)
            latched = opened_at is not None and now - opened_at < float(
                self.cfg.cb_cooldown_seconds
            )
            self._prune(shard, now)
            window = self._window.get(shard, [])
            total = len(window)
            healed = sum(1 for _, was_healed in window if was_healed)
            breakers[shard] = {
                "open": latched,
                "healed_steps": healed,
                "total_steps": total,
                "heal_rate": (healed / total) if total else 0.0,
                "window_seconds": float(self.cfg.cb_window_seconds),
                "cooldown_seconds": float(self.cfg.cb_cooldown_seconds),
            }
        return {"date": today, "budgets": budgets, "circuit_breakers": breakers}
