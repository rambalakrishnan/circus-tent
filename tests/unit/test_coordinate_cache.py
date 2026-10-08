"""Unit tests for circus_tent.engine.coordinate_cache."""

from __future__ import annotations

import json
import logging
from io import BytesIO

import pytest
from PIL import Image

from circus_tent.engine.coordinate_cache import (
    CoordinateCache,
    CoordinateEntry,
    compute_dhash,
)


def png_bytes(pixels: list[int], size: int = 32) -> bytes:
    img = Image.new("L", (size, size))
    img.putdata(pixels)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def gradient() -> bytes:
    return png_bytes([(x * 255) // 31 for _ in range(32) for x in range(32)])


def inverted_gradient() -> bytes:
    return png_bytes([255 - (x * 255) // 31 for _ in range(32) for x in range(32)])


def make_entry(**overrides: object) -> CoordinateEntry:
    fields: dict[str, object] = {
        "x": 412,
        "y": 587,
        "width": 140,
        "height": 40,
        "viewport": (1440, 900),
        "step_id": "submit_application",
    }
    fields.update(overrides)
    return CoordinateEntry(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_dhash_is_deterministic() -> None:
    raw = gradient()
    assert compute_dhash(raw) == compute_dhash(raw)


@pytest.mark.unit
def test_dhash_hex_length() -> None:
    raw = gradient()
    assert len(compute_dhash(raw)) == 64  # 16x16 bits → 64 hex chars
    assert len(compute_dhash(raw, size=8)) == 16
    assert len(compute_dhash(raw, size=4)) == 4


@pytest.mark.unit
def test_dhash_solid_color_is_zeros() -> None:
    raw = png_bytes([128] * (32 * 32))
    assert compute_dhash(raw) == "0" * 64


@pytest.mark.unit
def test_dhash_distinguishes_images() -> None:
    assert compute_dhash(gradient()) != compute_dhash(inverted_gradient())


@pytest.mark.unit
def test_dhash_handles_rgb_png() -> None:
    img = Image.new("RGB", (32, 32), (200, 30, 30))
    buf = BytesIO()
    img.save(buf, format="PNG")
    assert compute_dhash(buf.getvalue()) == "0" * 64  # solid → zeros


@pytest.mark.unit
def test_missing_file_loads_empty(tmp_path) -> None:
    cache = CoordinateCache(tmp_path / "coords.json", "workdayjobs.com")
    cache.load()
    assert cache.get("whatever", (1440, 900)) is None


@pytest.mark.unit
def test_round_trip_via_disk(tmp_path) -> None:
    cache = CoordinateCache(tmp_path / "coords.json", "workdayjobs.com")
    entry = make_entry(created_at="2026-10-08T00:00:00+00:00", hit_count=18)
    cache.record("abc123", entry)
    cache.save()

    data = json.loads((tmp_path / "coords.json").read_text())
    assert data["schema_version"] == 1
    assert data["domain"] == "workdayjobs.com"
    raw = data["entries"]["abc123"]
    assert raw["x"] == 412
    assert raw["y"] == 587
    assert raw["width"] == 140
    assert raw["height"] == 40
    assert raw["viewport"] == [1440, 900]
    assert raw["step_id"] == "submit_application"
    assert raw["hit_count"] == 18
    assert raw["created_at"] == "2026-10-08T00:00:00+00:00"

    reloaded = CoordinateCache(tmp_path / "coords.json", "workdayjobs.com")
    reloaded.load()
    got = reloaded.get("abc123", (1440, 900))
    assert got is not None
    assert got.x == 412
    assert got.y == 587
    assert got.width == 140
    assert got.height == 40
    assert got.viewport == (1440, 900)
    assert got.step_id == "submit_application"
    assert got.hit_count == 18


@pytest.mark.unit
def test_get_requires_viewport_match(tmp_path) -> None:
    cache = CoordinateCache(tmp_path / "coords.json", "d")
    cache.load()
    cache.record("h1", make_entry(viewport=(1440, 900)))
    assert cache.get("h1", (1440, 900)) is not None
    assert cache.get("h1", (1920, 1080)) is None
    assert cache.get("h1", (900, 1440)) is None  # dimensions differ


@pytest.mark.unit
def test_get_requires_hash_match(tmp_path) -> None:
    cache = CoordinateCache(tmp_path / "coords.json", "d")
    cache.load()
    cache.record("h1", make_entry())
    assert cache.get("h2", (1440, 900)) is None


@pytest.mark.unit
def test_save_is_atomic(tmp_path) -> None:
    cache = CoordinateCache(tmp_path / "coords.json", "d")
    cache.record("h1", make_entry())
    cache.save()
    assert not (tmp_path / "coords.tmp").exists()
    json.loads((tmp_path / "coords.json").read_text())


@pytest.mark.unit
def test_corrupt_file_warns_backs_up_starts_empty(tmp_path, caplog) -> None:
    path = tmp_path / "coords.json"
    path.write_text("this is not json")
    cache = CoordinateCache(path, "d")
    with caplog.at_level(logging.WARNING):
        cache.load()
    assert cache.get("h1", (1440, 900)) is None
    assert (tmp_path / "coords.json.bak").exists()
    assert any("coordinate cache" in record.message for record in caplog.records)


@pytest.mark.unit
def test_wrong_schema_version_backs_up_starts_empty(tmp_path, caplog) -> None:
    path = tmp_path / "coords.json"
    path.write_text(json.dumps({"schema_version": 2, "domain": "d", "entries": {}}))
    cache = CoordinateCache(path, "d")
    with caplog.at_level(logging.WARNING):
        cache.load()
    assert cache.get("h1", (1440, 900)) is None
    assert (tmp_path / "coords.json.bak").exists()
    assert any("coordinate cache" in record.message for record in caplog.records)
