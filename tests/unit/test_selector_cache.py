"""Unit tests for circus_tent.engine.selector_cache."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest

from circus_tent.engine.selector_cache import SelectorCache, SelectorEntry


def make_entry(**overrides: object) -> SelectorEntry:
    fields: dict[str, object] = {
        "selector": "#email",
        "strategy": "css",
        "confidence": 0.95,
    }
    fields.update(overrides)
    return SelectorEntry(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_missing_file_loads_empty(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "selectors.json", "workdayjobs.com", 1)
    cache.load()
    assert cache.entries() == {}


@pytest.mark.unit
def test_save_writes_expected_shape(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "selectors.json", "workdayjobs.com", 3)
    cache.record_heal("submit_application", make_entry())
    cache.record_hit("submit_application", now="2026-10-08T00:00:00+00:00")
    cache.save()

    data = json.loads((tmp_path / "selectors.json").read_text())
    assert data["schema_version"] == 1
    assert data["domain"] == "workdayjobs.com"
    assert data["version"] == "2026.10"
    assert data["prompt_version"] == 3
    assert data["last_verified"] == "2026-10-08T00:00:00+00:00"
    step = data["steps"]["submit_application"]
    assert step["selector"] == "#email"
    assert step["strategy"] == "css"
    assert step["stale"] is False
    assert step["heal_count"] == 1
    assert step["confidence"] == 0.9
    assert step["hit_count"] == 1


@pytest.mark.unit
def test_save_is_atomic(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "selectors.json", "d", 1)
    cache.record_heal("s1", make_entry())
    cache.save()
    assert not (tmp_path / "selectors.tmp").exists()
    data = json.loads((tmp_path / "selectors.json").read_text())
    assert "s1" in data["steps"]


@pytest.mark.unit
def test_save_creates_parent_dirs(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "a" / "b" / "selectors.json", "d", 1)
    cache.record_heal("s1", make_entry())
    cache.save()
    assert (tmp_path / "a" / "b" / "selectors.json").exists()


@pytest.mark.unit
def test_round_trip_via_disk(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "selectors.json", "workdayjobs.com", 2)
    entry = make_entry(selector="//input[@id='email']", strategy="xpath", confidence=0.81)
    cache.record_heal("login", entry)
    cache.record_hit("login", now="2026-10-08T01:00:00+00:00")
    cache.mark_stale("login")
    cache.save()

    reloaded = SelectorCache(tmp_path / "selectors.json", "workdayjobs.com", 2)
    reloaded.load()
    got = reloaded.get("login")
    assert got is not None
    assert got.selector == "//input[@id='email']"
    assert got.strategy == "xpath"
    assert got.confidence == 0.76  # 0.81 - heal decay 0.05
    assert got.stale is True
    assert got.hit_count == 1
    assert got.last_verified == "2026-10-08T01:00:00+00:00"


@pytest.mark.unit
def test_corrupt_file_warns_backs_up_starts_empty(tmp_path, caplog) -> None:
    path = tmp_path / "selectors.json"
    path.write_text("{not valid json!!!")
    cache = SelectorCache(path, "d", 1)
    with caplog.at_level(logging.WARNING):
        cache.load()
    assert cache.entries() == {}
    bak = tmp_path / "selectors.json.bak"
    assert bak.exists()
    assert bak.read_text() == "{not valid json!!!"
    assert any("selector cache" in record.message for record in caplog.records)


@pytest.mark.unit
def test_wrong_schema_version_backs_up_starts_empty(tmp_path, caplog) -> None:
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps({"schema_version": 999, "domain": "d", "steps": {}}))
    cache = SelectorCache(path, "d", 1)
    with caplog.at_level(logging.WARNING):
        cache.load()
    assert cache.entries() == {}
    assert (tmp_path / "selectors.json.bak").exists()
    assert any("selector cache" in record.message for record in caplog.records)


@pytest.mark.unit
def test_corrupt_load_then_save_writes_fresh_file(tmp_path) -> None:
    path = tmp_path / "selectors.json"
    path.write_text("garbage")
    cache = SelectorCache(path, "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry())
    cache.save()
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert "s1" in data["steps"]


@pytest.mark.unit
def test_get_returns_none_for_unknown_step(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    assert cache.get("missing") is None


@pytest.mark.unit
def test_get_returns_stale_when_no_live_entry(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry())
    cache.mark_stale("s1")
    got = cache.get("s1")
    assert got is not None
    assert got.stale is True


@pytest.mark.unit
def test_record_hit_increments_and_stamps(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry())
    cache.record_hit("s1", now="2026-10-08T02:00:00+00:00")
    entry = cache.get("s1")
    assert entry is not None
    assert entry.hit_count == 1
    assert entry.last_verified == "2026-10-08T02:00:00+00:00"
    cache.record_hit("s1")
    assert entry.hit_count == 2
    # Without an explicit `now`, the stamp refreshes to a fresh UTC timestamp.
    assert entry.last_verified.endswith("+00:00")
    assert datetime.fromisoformat(entry.last_verified).tzinfo is not None


@pytest.mark.unit
def test_record_hit_timestamp_is_utc_iso_seconds(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry())
    cache.record_hit("s1")
    entry = cache.get("s1")
    assert entry is not None
    ts = entry.last_verified
    assert ts.endswith("+00:00")
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    assert parsed.microsecond == 0


@pytest.mark.unit
def test_record_hit_on_unknown_step_is_noop(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_hit("missing", now="2026-10-08T00:00:00+00:00")
    assert cache.entries() == {}


@pytest.mark.unit
def test_record_heal_decrements_confidence(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry(confidence=0.95))
    assert cache.get("s1").confidence == 0.9  # type: ignore[union-attr]
    cache.record_heal("s1", make_entry(confidence=0.9))
    assert cache.get("s1").confidence == 0.85  # type: ignore[union-attr]


@pytest.mark.unit
def test_record_heal_floors_confidence_at_010(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry(confidence=0.12))
    assert cache.get("s1").confidence == 0.1  # type: ignore[union-attr]
    cache.record_heal("s1", make_entry(confidence=0.1))
    assert cache.get("s1").confidence == 0.1  # type: ignore[union-attr]


@pytest.mark.unit
def test_record_heal_increments_heal_count_and_clears_stale(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry(heal_count=2, stale=True))
    entry = cache.get("s1")
    assert entry is not None
    assert entry.heal_count == 3
    assert entry.stale is False
    # Heal again with a caller-constructed entry; count keeps accruing.
    cache.record_heal("s1", make_entry())
    assert cache.get("s1").heal_count == 4  # type: ignore[union-attr]


@pytest.mark.unit
def test_mark_stale(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry())
    cache.mark_stale("s1")
    assert cache.get("s1").stale is True  # type: ignore[union-attr]
    cache.mark_stale("missing")  # no-op, must not raise


@pytest.mark.unit
def test_demote_by_prompt_version_marks_all_stale(tmp_path) -> None:
    cache = SelectorCache(tmp_path / "s.json", "d", 1)
    cache.load()
    cache.record_heal("s1", make_entry())
    cache.record_heal("s2", make_entry())
    cache.demote_by_prompt_version()
    assert all(entry.stale for entry in cache.entries().values())
    # Fresh heals are live again; a second demote re-marks them.
    cache.record_heal("s1", make_entry())
    assert cache.get("s1").stale is False  # type: ignore[union-attr]
    cache.demote_by_prompt_version()
    assert all(entry.stale for entry in cache.entries().values())
