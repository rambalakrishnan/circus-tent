"""Callback delivery: signed terminal-state POSTs (C). See spec."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from circus_tent.telemetry import Metrics

_RETRY_DELAYS = (1.0, 2.0, 4.0)
_MAX_PENDING = 100


class CallbackClient:
    def __init__(
        self,
        sign_key: str,
        metrics: Metrics,
        logger: logging.Logger,
        max_retries: int = 3,
    ) -> None:
        self._sign_key = sign_key.encode()
        self.metrics = metrics
        self.logger = logger
        self._max_retries = max_retries
        self._pending = 0
        self._client = httpx.AsyncClient(timeout=10.0)

    def enqueue(self, callback_url: str, payload: dict[str, Any], traceparent: str | None) -> None:
        """Fire-and-forget with a bounded background queue — never blocks the
        HTTP response."""
        if self._pending >= _MAX_PENDING:
            self.metrics.callback_failures.inc()
            self.logger.warning(
                "callback queue full, dropping delivery",
                extra={"event": "callback_dropped", "url": callback_url},
            )
            return
        self._pending += 1
        asyncio.get_running_loop().create_task(self._deliver(callback_url, payload, traceparent))

    async def _deliver(
        self, callback_url: str, payload: dict[str, Any], traceparent: str | None
    ) -> None:
        try:
            ok = await self.notify(callback_url, payload, traceparent)
            if not ok:
                self.metrics.callback_failures.inc()
        finally:
            self._pending -= 1

    async def notify(
        self, callback_url: str, payload: dict[str, Any], traceparent: str | None = None
    ) -> bool:
        body = json.dumps(payload).encode()
        signature = hmac.new(self._sign_key, body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Circus-Tent-Signature": f"sha256={signature}",
        }
        if traceparent:
            headers["traceparent"] = traceparent
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.post(callback_url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    return True
                last_error = Exception(f"callback returned HTTP {resp.status_code}")
            except Exception as e:  # noqa: BLE001
                last_error = e
            if attempt < self._max_retries - 1:
                await asyncio.sleep(_RETRY_DELAYS[attempt])
        self.logger.warning(
            "callback delivery failed",
            extra={"event": "callback_failed", "url": callback_url, "error": str(last_error)},
        )
        return False
