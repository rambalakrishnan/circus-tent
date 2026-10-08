"""Audit log: JSONL binding calls to caller identity (B1). See spec."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from circus_tent.telemetry import Metrics


class AuditLog:
    def __init__(self, path: Path, metrics: Metrics, logger: logging.Logger) -> None:
        self.path = path
        self.metrics = metrics
        self.logger = logger
        self._lock = asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)

    async def record(self, **fields: object) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            **fields,
        }
        async with self._lock:
            try:
                with self.path.open("a") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
            except OSError as e:
                self.logger.warning(
                    "audit write failed", extra={"event": "audit_write_failed", "error": str(e)}
                )
                return
        self.metrics.audit_events.inc()
