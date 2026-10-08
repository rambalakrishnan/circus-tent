"""Coordinate cache: dHash(png) → bounding box. See spec."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)


def compute_dhash(png_bytes: bytes, size: int = 16) -> str:
    """dHash perceptual hash (hex) of a PNG byte string.

    Grayscale → resize to (size+1) × size → row-difference bits → hex string.
    Deterministic for identical pixels; robust to minor compression noise.
    """
    with Image.open(BytesIO(png_bytes)) as img:
        resized = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
        pixels = resized.tobytes()
    bits = 0
    for row in range(size):
        row_start = row * (size + 1)
        for col in range(size):
            left = pixels[row_start + col]
            right = pixels[row_start + col + 1]
            bits = (bits << 1) | (1 if left < right else 0)
    return format(bits, f"0{size * size // 4}x")


@dataclass
class CoordinateEntry:
    x: int
    y: int
    width: int
    height: int
    viewport: tuple[int, int]
    step_id: str
    hit_count: int = 0
    created_at: str = ""


class CoordinateCache:
    def __init__(self, path: Path, domain: str) -> None:
        self.path = path
        self.domain = domain
        self._entries: dict[str, CoordinateEntry] = {}
        self._loaded = False

    def load(self) -> None:
        self._entries = {}
        if not self.path.exists():
            self._loaded = True
            return
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
            self._check_schema(data)
            entries_raw = data.get("entries")
            entries = entries_raw if isinstance(entries_raw, dict) else {}
            for phash, raw in entries.items():
                if not isinstance(phash, str):
                    raise ValueError(f"phash is not a string: {phash!r}")
                self._entries[phash] = self._entry_from_raw(raw)
        except Exception as exc:
            logger.warning(
                "coordinate cache %s is unusable (%s); backing up to .bak and starting empty",
                self.path,
                exc,
            )
            self._back_up()
            self._entries = {}
        self._loaded = True

    def get(self, phash: str, viewport: tuple[int, int]) -> CoordinateEntry | None:
        # Hash match AND viewport match — different viewport = different coords.
        entry = self._entries.get(phash)
        if entry is None:
            return None
        return entry if tuple(entry.viewport) == tuple(viewport) else None

    def record(self, phash: str, entry: CoordinateEntry) -> None:
        self._entries[phash] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "domain": self.domain,
            "entries": {phash: self._entry_to_raw(entry) for phash, entry in self._entries.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    # -- internals -------------------------------------------------------------

    def _check_schema(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("cache root is not a JSON object")
        if "schema_version" in data and data["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {data['schema_version']!r}")

    def _entry_from_raw(self, raw: Any) -> CoordinateEntry:
        if not isinstance(raw, dict):
            raise ValueError("entry is not a JSON object")
        x = raw.get("x")
        y = raw.get("y")
        width = raw.get("width")
        height = raw.get("height")
        if (
            not isinstance(x, int)
            or isinstance(x, bool)
            or not isinstance(y, int)
            or isinstance(y, bool)
            or not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
        ):
            raise ValueError("entry has missing/invalid coordinates")
        step_id = raw.get("step_id")
        if not isinstance(step_id, str):
            raise ValueError("entry is missing step_id")
        viewport_raw = raw.get("viewport")
        if (
            not isinstance(viewport_raw, list)
            or len(viewport_raw) != 2
            or not isinstance(viewport_raw[0], int)
            or isinstance(viewport_raw[0], bool)
            or not isinstance(viewport_raw[1], int)
            or isinstance(viewport_raw[1], bool)
        ):
            raise ValueError("entry has missing/invalid viewport")
        return CoordinateEntry(
            x=x,
            y=y,
            width=width,
            height=height,
            viewport=(viewport_raw[0], viewport_raw[1]),
            step_id=step_id,
            hit_count=int(raw.get("hit_count", 0)),
            created_at=str(raw.get("created_at", "")),
        )

    def _entry_to_raw(self, entry: CoordinateEntry) -> dict[str, Any]:
        raw = asdict(entry)
        raw["viewport"] = [entry.viewport[0], entry.viewport[1]]
        return raw

    def _back_up(self) -> None:
        bak = Path(str(self.path) + ".bak")
        try:
            os.replace(self.path, bak)
        except OSError:
            logger.warning("failed to back up unusable cache %s", self.path)
