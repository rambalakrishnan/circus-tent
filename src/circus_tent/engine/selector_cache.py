"""Selector cache: versioned, atomic, confidence-scored. See spec."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
FORMAT_VERSION = "2026.10"
CONFIDENCE_DECAY = 0.05
CONFIDENCE_FLOOR = 0.10

logger = logging.getLogger(__name__)


def _now() -> str:
    """UTC timestamp, ISO8601, seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class SelectorEntry:
    selector: str
    strategy: str  # "css" | "xpath"
    confidence: float  # 0.0..1.0
    hit_count: int = 0
    heal_count: int = 0
    stale: bool = False
    fallback_text: str = ""
    last_verified: str = ""  # ISO8601


class SelectorCache:
    def __init__(self, path: Path, domain: str, prompt_version: int) -> None:
        self.path = path
        self.domain = domain
        self.prompt_version = prompt_version
        self._entries: dict[str, SelectorEntry] = {}
        self._loaded = False

    def load(self) -> None:
        self._entries = {}
        if not self.path.exists():
            self._loaded = True
            return
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
            self._check_schema(data)
            steps_raw = data.get("steps")
            steps = steps_raw if isinstance(steps_raw, dict) else {}
            for step_id, raw in steps.items():
                if not isinstance(step_id, str):
                    raise ValueError(f"step id is not a string: {step_id!r}")
                self._entries[step_id] = self._entry_from_raw(raw)
        except Exception as exc:
            logger.warning(
                "selector cache %s is unusable (%s); backing up to .bak and starting empty",
                self.path,
                exc,
            )
            self._back_up()
            self._entries = {}
        self._loaded = True

    def get(self, step_id: str) -> SelectorEntry | None:
        # One entry per step: live entry preferred; stale returned only when
        # no live entry exists for the step.
        return self._entries.get(step_id)

    def record_hit(self, step_id: str, now: str | None = None) -> None:
        entry = self._entries.get(step_id)
        if entry is None:
            return
        entry.hit_count += 1
        entry.last_verified = now if now is not None else _now()

    def record_heal(self, step_id: str, entry: SelectorEntry) -> None:
        prev = self._entries.get(step_id)
        entry.heal_count = (prev.heal_count if prev is not None else entry.heal_count) + 1
        entry.confidence = round(max(CONFIDENCE_FLOOR, entry.confidence - CONFIDENCE_DECAY), 2)
        entry.stale = False
        self._entries[step_id] = entry

    def mark_stale(self, step_id: str) -> None:
        entry = self._entries.get(step_id)
        if entry is None:
            return
        entry.stale = True

    def demote_by_prompt_version(self) -> None:
        for entry in self._entries.values():
            entry.stale = True

    def entries(self) -> dict[str, SelectorEntry]:
        return self._entries

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "domain": self.domain,
            "version": FORMAT_VERSION,
            "prompt_version": self.prompt_version,
            "last_verified": self._last_verified(),
            "steps": {step_id: asdict(entry) for step_id, entry in self._entries.items()},
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

    def _entry_from_raw(self, raw: Any) -> SelectorEntry:
        if not isinstance(raw, dict):
            raise ValueError("step entry is not a JSON object")
        selector = raw.get("selector")
        strategy = raw.get("strategy")
        if not isinstance(selector, str) or not isinstance(strategy, str):
            raise ValueError("step entry is missing selector/strategy")
        return SelectorEntry(
            selector=selector,
            strategy=strategy,
            confidence=float(raw.get("confidence", 1.0)),
            hit_count=int(raw.get("hit_count", 0)),
            heal_count=int(raw.get("heal_count", 0)),
            stale=bool(raw.get("stale", False)),
            fallback_text=str(raw.get("fallback_text", "")),
            last_verified=str(raw.get("last_verified", "")),
        )

    def _last_verified(self) -> str:
        stamps = [e.last_verified for e in self._entries.values() if e.last_verified]
        return max(stamps) if stamps else ""

    def _back_up(self) -> None:
        bak = Path(str(self.path) + ".bak")
        try:
            os.replace(self.path, bak)
        except OSError:
            logger.warning("failed to back up unusable cache %s", self.path)
