"""Execution ledger: idempotency, checkpoints, side-effect at-most-once. See spec."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class LedgerCheckpoint:
    step_id: str
    index: int  # 0-based index in the step list
    note: str = ""
    created_at: str = ""


@dataclass
class LedgerRun:
    run_id: str
    idempotency_key: str
    account: str
    shard: str
    domain: str
    status: str  # running | completed | failed
    step_states: dict[str, str] = field(default_factory=dict)  # step_id → ok|healed|...
    checkpoints: list[LedgerCheckpoint] = field(default_factory=list)
    side_effect_completed: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


class ExecutionLedger:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._locks: dict[str, asyncio.Lock] = {}
        self._runs: dict[str, LedgerRun] = {}
        base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, idempotency_key: str) -> Path:
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        return self.base_dir / f"{key_hash}.json"

    def _lock_for(self, idempotency_key: str) -> asyncio.Lock:
        lock = self._locks.get(idempotency_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[idempotency_key] = lock
        return lock

    def get_by_key(self, idempotency_key: str) -> LedgerRun | None:
        path = self._path_for(idempotency_key)
        if not path.exists():
            return None
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
            self._check_schema(data)
            run = self._run_from_raw(data)
        except Exception as exc:
            logger.warning(
                "ledger file %s is unusable (%s); backing up to .bak",
                path,
                exc,
            )
            self._back_up(path)
            return None
        self._runs[run.run_id] = run
        return run

    def create(
        self,
        run_id: str,
        idempotency_key: str,
        account: str,
        shard: str,
        domain: str,
        steps: Sequence[str],
    ) -> LedgerRun:
        # Retry with the same key resumes the existing run.
        existing = self.get_by_key(idempotency_key)
        if existing is not None:
            return existing
        run = LedgerRun(
            run_id=run_id,
            idempotency_key=idempotency_key,
            account=account,
            shard=shard,
            domain=domain,
            status="running",
            step_states={step: "pending" for step in steps},
        )
        self._runs[run_id] = run
        self.save(run_id)
        return run

    def record_step(self, run_id: str, step_id: str, status: str) -> None:
        run = self._runs.get(run_id)
        if run is None or step_id not in run.step_states:
            return
        run.step_states[step_id] = status
        run.updated_at = _now()
        self.save(run_id)

    def record_checkpoint(self, run_id: str, step_id: str, index: int, note: str = "") -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.checkpoints.append(
            LedgerCheckpoint(step_id=step_id, index=index, note=note, created_at=_now())
        )
        run.updated_at = _now()
        self.save(run_id)

    def mark_side_effect_done(self, run_id: str, step_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        if step_id not in run.side_effect_completed:
            run.side_effect_completed.append(step_id)
        run.updated_at = _now()
        self.save(run_id)

    def finalize(self, run_id: str, status: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run.status = status
        run.updated_at = _now()
        self.save(run_id)

    def last_checkpoint(self, run_id: str) -> LedgerCheckpoint | None:
        run = self._runs.get(run_id)
        if run is None or not run.checkpoints:
            return None
        return run.checkpoints[-1]

    def save(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        path = self._path_for(run.idempotency_key)
        data: dict[str, Any] = asdict(run)
        data["schema_version"] = SCHEMA_VERSION
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # -- internals -------------------------------------------------------------

    def _check_schema(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("ledger file root is not a JSON object")
        if "schema_version" in data and data["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {data['schema_version']!r}")

    def _run_from_raw(self, data: dict[str, Any]) -> LedgerRun:
        checkpoints: list[LedgerCheckpoint] = []
        raw_checkpoints = data.get("checkpoints", [])
        if isinstance(raw_checkpoints, list):
            for raw in raw_checkpoints:
                if isinstance(raw, dict):
                    checkpoints.append(
                        LedgerCheckpoint(
                            step_id=str(raw.get("step_id", "")),
                            index=int(raw.get("index", 0)),
                            note=str(raw.get("note", "")),
                            created_at=str(raw.get("created_at", "")),
                        )
                    )
        raw_states = data.get("step_states")
        step_states: dict[str, str] = (
            {str(k): str(v) for k, v in raw_states.items()} if isinstance(raw_states, dict) else {}
        )
        raw_side = data.get("side_effect_completed")
        side_effect_completed: list[str] = (
            [str(s) for s in raw_side] if isinstance(raw_side, list) else []
        )
        return LedgerRun(
            run_id=str(data.get("run_id", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
            account=str(data.get("account", "")),
            shard=str(data.get("shard", "")),
            domain=str(data.get("domain", "")),
            status=str(data.get("status", "")),
            step_states=step_states,
            checkpoints=checkpoints,
            side_effect_completed=side_effect_completed,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    def _back_up(self, path: Path) -> None:
        bak = Path(str(path) + ".bak")
        try:
            os.replace(path, bak)
        except OSError:
            logger.warning("failed to back up unusable ledger file %s", path)
