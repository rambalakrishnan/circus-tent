"""Service wiring shared by REST and MCP. See spec."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from circus_tent.api.audit import AuditLog
from circus_tent.api.auth import CallerAuth
from circus_tent.api.callbacks import CallbackClient
from circus_tent.api.fairness import FairShare
from circus_tent.browser.cluster_manager import ClusterManager
from circus_tent.config.loader import CallerConfig, ConfigError, ConfigLoader
from circus_tent.engine.budgets import BudgetManager
from circus_tent.engine.ledger import ExecutionLedger
from circus_tent.engine.models import ModelClient
from circus_tent.engine.state_machine import (
    CoordinateCacheStore,
    ExecutionEngine,
    RunResult,
    SelectorCacheStore,
)
from circus_tent.parser.extraction import ExtractionResult
from circus_tent.security.challenge_resolver import CapSolverClient, ChallengeResolver
from circus_tent.telemetry import get_logger, get_metrics


class Service:
    def __init__(self, config_dir: Path, env: Mapping[str, str] | None = None) -> None:
        self.config_dir = Path(config_dir)
        self.logger = get_logger("circus_tent.service")
        self.metrics = get_metrics()
        self.loader = ConfigLoader(self.config_dir, env)
        models = self.loader.load_models()
        shards = self.loader.load_shards()
        self.accounts = {a.id: a for a in self.loader.load_accounts()}

        profiles_root = Path(shards[0].profile_dir).parent
        ledger_dir = profiles_root.parent / "ledger"

        self.budgets = BudgetManager(models.budgets, self.metrics, self.logger)
        self.cluster = ClusterManager(self.loader, self.metrics, self.logger)
        self.caches = SelectorCacheStore(self.config_dir / "selectors", models.prompt_version)
        self.coord_caches = CoordinateCacheStore(self.config_dir / "coordinates")
        self.ledger = ExecutionLedger(ledger_dir)

        challenge_resolver: ChallengeResolver | None = None
        try:
            capsolver_key = self.loader.resolve_secret("CAPSOLVER_API_KEY")
            if capsolver_key:
                client = CapSolverClient(capsolver_key, self.metrics, self.logger)
                challenge_resolver = ChallengeResolver(client, self.metrics, self.logger)
        except ConfigError:
            challenge_resolver = None

        self.model_client = ModelClient(models, self.metrics, self.budgets, self.logger)
        self.engine = ExecutionEngine(
            self.cluster,
            self.caches,
            self.coord_caches,
            self.model_client,
            self.ledger,
            self.budgets,
            challenge_resolver,
            self.metrics,
            self.logger,
        )
        self.fairness = {shard.name: FairShare(shard.max_tabs, default_quota=3) for shard in shards}
        self.auth = CallerAuth(self.loader.load_callers(), self.loader.resolve_secret, self.metrics)
        sign_key = self._callback_sign_key()
        self.audit = AuditLog(
            profiles_root.parent / "audit" / "audit.jsonl", self.metrics, self.logger
        )
        self.callbacks = CallbackClient(sign_key, self.metrics, self.logger)

    def _callback_sign_key(self) -> str:
        try:
            return self.loader.resolve_secret("AUTOMATION_WRAPPER_API_KEY")
        except ConfigError:
            callers = self.loader.load_callers()
            for caller in callers:
                try:
                    return self.loader.resolve_secret(caller.key_env)
                except ConfigError:
                    continue
        self.logger.warning(
            "no signing key configured for callbacks",
            extra={"event": "callback_signing_key_missing"},
        )
        return "circus-tent-unsigned"

    async def start(self, warmup: bool = False) -> None:
        await self.cluster.start(warmup=warmup)

    async def shutdown(self, drain_seconds: float = 30.0) -> int:
        return await self.cluster.shutdown(drain_seconds)

    def _account_quota(self, account: str | None) -> int:
        if account and account in self.accounts:
            return self.accounts[account].quota
        return 3

    async def run(self, caller: CallerConfig, req: dict[str, Any]) -> RunResult:
        domain = str(req.get("domain") or "")
        account = req.get("account")
        shard = self.cluster.shard_for_domain(domain)
        self.auth.check_scope(caller, "run", shard.name, account)
        await self.auth.check_rate(caller)

        idem = str(req.get("idempotency_key") or "")
        trace_id = req.get("_traceparent")
        started = time.monotonic()
        await self.audit.record(
            caller_id=caller.id,
            tool="run",
            domain=domain,
            account=account,
            idempotency_key=idem,
            status="running",
            trace_id=trace_id,
        )
        fairness = self.fairness[shard.name]
        await fairness.acquire(account or "default", self._account_quota(account))
        try:
            result = await self.engine.run(req)
        finally:
            fairness.release(account or "default")
        duration_ms = int((time.monotonic() - started) * 1000)
        await self.audit.record(
            caller_id=caller.id,
            tool="run",
            domain=domain,
            account=account,
            idempotency_key=idem,
            status=result.status,
            duration_ms=duration_ms,
            trace_id=trace_id,
        )
        callback_url = req.get("callback_url")
        if callback_url:
            self.callbacks.enqueue(
                str(callback_url),
                {
                    "run_id": result.run_id,
                    "idempotency_key": result.idempotency_key,
                    "status": result.status,
                    "steps": [
                        {"id": s.id, "status": s.status, "error": s.error} for s in result.steps
                    ],
                },
                trace_id,
            )
        return result

    async def extract(self, caller: CallerConfig, req: dict[str, Any]) -> ExtractionResult:
        domain = str(req.get("domain") or "")
        shard = self.cluster.shard_for_domain(domain)
        self.auth.check_scope(caller, "extract", shard.name, None)
        await self.auth.check_rate(caller)
        trace_id = req.get("_traceparent")
        await self.audit.record(
            caller_id=caller.id,
            tool="extract",
            domain=domain,
            account=None,
            idempotency_key=None,
            status="running",
            trace_id=trace_id,
        )
        result = await self.engine.extract_url(
            domain, str(req.get("url") or ""), req.get("query"), req.get("schema")
        )
        await self.audit.record(
            caller_id=caller.id,
            tool="extract",
            domain=domain,
            account=None,
            idempotency_key=None,
            status="completed",
            trace_id=trace_id,
        )
        return result

    def health(self) -> dict[str, Any]:
        return {
            "api_version": "v1",
            "status": "ok",
            "shards": [
                {
                    "name": s.health.name,
                    "state": s.health.state.value,
                    "tabs_active": s.health.tabs_active,
                    "pages_processed": s.health.pages_processed,
                    "fingerprint_ok": s.health.fingerprint_ok,
                    "cooldown_until": s.health.cooldown_until,
                    "last_error": s.health.last_error,
                }
                for s in self.cluster.snapshots()
            ],
            "queues": {
                "depth_total": sum(fs.queue_depth() for fs in self.fairness.values()),
                "per_shard": {k: fs.queue_depth() for k, fs in self.fairness.items()},
            },
            "budgets": self.budgets.snapshot(),
        }

    def shards(self) -> dict[str, Any]:
        return {
            "api_version": "v1",
            "shards": [
                {
                    "name": s.config_name,
                    "patterns": list(s.patterns),
                    "state": s.health.state.value,
                    "tabs_active": s.health.tabs_active,
                    "tabs_max": 12,
                    "pages_processed": s.health.pages_processed,
                    "fingerprint_ok": s.health.fingerprint_ok,
                }
                for s in self.cluster.snapshots()
            ],
        }
