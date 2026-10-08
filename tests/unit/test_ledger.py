"""Unit tests for circus_tent.engine.ledger."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from circus_tent.engine.ledger import ExecutionLedger, LedgerRun

KEY = "order-123"


def key_path(base: Path, idempotency_key: str = KEY) -> Path:
    return base / f"{hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]}.json"


@pytest.mark.unit
def test_create_initializes_pending_and_writes_file(tmp_path) -> None:
    base = tmp_path / "ledger"
    ledger = ExecutionLedger(base)
    run = ledger.create("run1", KEY, "acct1", "shard1", "workdayjobs.com", ["login", "apply"])
    assert run.status == "running"
    assert run.step_states == {"login": "pending", "apply": "pending"}
    assert run.checkpoints == []
    assert run.side_effect_completed == []

    path = key_path(base)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["run_id"] == "run1"
    assert data["idempotency_key"] == KEY
    assert data["account"] == "acct1"
    assert data["shard"] == "shard1"
    assert data["domain"] == "workdayjobs.com"
    assert data["status"] == "running"
    assert data["step_states"] == {"login": "pending", "apply": "pending"}
    assert data["checkpoints"] == []
    assert data["side_effect_completed"] == []


@pytest.mark.unit
def test_get_by_key_returns_none_for_unknown(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    assert ledger.get_by_key("nope") is None
    assert ledger.get_by_key(KEY) is None


@pytest.mark.unit
def test_get_by_key_works_when_dir_removed(tmp_path) -> None:
    base = tmp_path / "ledger"
    ledger = ExecutionLedger(base)
    ledger.create("run1", KEY, "a", "s", "d", ["x"])
    shutil.rmtree(base)
    assert ledger.get_by_key(KEY) is None


@pytest.mark.unit
def test_record_step_updates_and_persists(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run1", KEY, "a", "s", "d", ["login", "apply"])
    ledger.record_step("run1", "login", "ok")
    data = json.loads(key_path(tmp_path / "ledger").read_text())
    assert data["step_states"] == {"login": "ok", "apply": "pending"}


@pytest.mark.unit
def test_record_step_on_unknown_ids_is_noop(tmp_path) -> None:
    base = tmp_path / "ledger"
    ledger = ExecutionLedger(base)
    ledger.create("run1", KEY, "a", "s", "d", ["login"])
    before = json.loads(key_path(base).read_text())
    ledger.record_step("missing-run", "login", "ok")
    ledger.record_step("run1", "missing-step", "ok")
    after = json.loads(key_path(base).read_text())
    assert after == before


@pytest.mark.unit
def test_record_checkpoint_and_last_checkpoint(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run1", KEY, "a", "s", "d", ["s1", "s2", "s3"])
    assert ledger.last_checkpoint("run1") is None
    ledger.record_checkpoint("run1", "s1", 0)
    ledger.record_checkpoint("run1", "s2", 1, note="after login")
    cp = ledger.last_checkpoint("run1")
    assert cp is not None
    assert cp.step_id == "s2"
    assert cp.index == 1
    assert cp.note == "after login"
    assert cp.created_at

    data = json.loads(key_path(tmp_path / "ledger").read_text())
    assert [c["index"] for c in data["checkpoints"]] == [0, 1]
    assert data["checkpoints"][1]["step_id"] == "s2"
    assert data["checkpoints"][1]["note"] == "after login"
    assert data["checkpoints"][1]["created_at"]


@pytest.mark.unit
def test_last_checkpoint_unknown_run_is_none(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run1", KEY, "a", "s", "d", ["s1"])
    assert ledger.last_checkpoint("missing") is None


@pytest.mark.unit
def test_mark_side_effect_done_dedupes(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run1", KEY, "a", "s", "d", ["submit"])
    ledger.mark_side_effect_done("run1", "submit")
    ledger.mark_side_effect_done("run1", "submit")
    run = ledger.get_by_key(KEY)
    assert run is not None
    assert run.side_effect_completed == ["submit"]
    ledger.mark_side_effect_done("missing-run", "submit")  # no-op


@pytest.mark.unit
def test_finalize_sets_status_and_persists(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run1", KEY, "a", "s", "d", ["s1"])
    ledger.finalize("run1", "completed")
    data = json.loads(key_path(tmp_path / "ledger").read_text())
    assert data["status"] == "completed"
    ledger.finalize("run1", "failed")
    assert json.loads(key_path(tmp_path / "ledger").read_text())["status"] == "failed"
    ledger.finalize("missing-run", "failed")  # no-op


@pytest.mark.unit
def test_resume_from_another_ledger_instance(tmp_path) -> None:
    base = tmp_path / "ledger"
    ledger1 = ExecutionLedger(base)
    ledger1.create("run1", KEY, "a", "s", "d", ["s1", "s2"])
    ledger1.record_step("run1", "s1", "ok")
    ledger1.record_checkpoint("run1", "s1", 0)
    ledger1.finalize("run1", "failed")

    ledger2 = ExecutionLedger(base)
    run = ledger2.get_by_key(KEY)
    assert run is not None
    assert run.run_id == "run1"
    assert run.status == "failed"
    assert run.step_states["s1"] == "ok"
    assert run.step_states["s2"] == "pending"
    assert run.checkpoints[-1].index == 0
    # A resumed ledger instance can continue recording.
    ledger2.record_step("run1", "s2", "healed")
    ledger3 = ExecutionLedger(base)
    assert ledger3.get_by_key(KEY).step_states["s2"] == "healed"  # type: ignore[union-attr]


@pytest.mark.unit
def test_create_with_same_key_returns_existing_run(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    ledger.create("run1", KEY, "a", "s", "d", ["x"])
    ledger.finalize("run1", "completed")
    second = ledger.create("run2", KEY, "other", "s2", "d2", ["y"])
    assert second.run_id == "run1"
    assert second.status == "completed"
    # Only one file ever exists for the key.
    names = [p.name for p in (tmp_path / "ledger").glob("*.json")]
    assert names == [key_path(tmp_path / "ledger").name]


@pytest.mark.unit
def test_timestamps_are_utc_iso_seconds(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger")
    run: LedgerRun = ledger.create("run1", KEY, "a", "s", "d", ["s1"])
    for ts in (run.created_at, run.updated_at):
        assert ts.endswith("+00:00")
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None
        assert parsed.microsecond == 0


@pytest.mark.unit
def test_corrupt_ledger_file_warns_backs_up_returns_none(tmp_path, caplog) -> None:
    base = tmp_path / "ledger"
    ledger = ExecutionLedger(base)
    ledger.create("run1", KEY, "a", "s", "d", ["x"])
    path = key_path(base)
    path.write_text("garbage{{{")
    with caplog.at_level(logging.WARNING):
        assert ledger.get_by_key(KEY) is None
    assert Path(str(path) + ".bak").exists()
    assert any("ledger" in record.message for record in caplog.records)


@pytest.mark.unit
def test_wrong_schema_ledger_file_returns_none(tmp_path, caplog) -> None:
    base = tmp_path / "ledger"
    ledger = ExecutionLedger(base)
    ledger.create("run1", KEY, "a", "s", "d", ["x"])
    path = key_path(base)
    path.write_text(json.dumps({"schema_version": 42, "run_id": "run1"}))
    with caplog.at_level(logging.WARNING):
        assert ledger.get_by_key(KEY) is None
    assert Path(str(path) + ".bak").exists()


@pytest.mark.unit
def test_save_is_atomic_and_no_tmp_left(tmp_path) -> None:
    base = tmp_path / "ledger"
    ledger = ExecutionLedger(base)
    ledger.create("run1", KEY, "a", "s", "d", ["s1", "s2"])
    ledger.record_step("run1", "s1", "ok")
    ledger.record_checkpoint("run1", "s1", 0)
    ledger.finalize("run1", "completed")
    assert list(base.glob("*.tmp")) == []
    data = json.loads(key_path(base).read_text())
    assert data["status"] == "completed"
