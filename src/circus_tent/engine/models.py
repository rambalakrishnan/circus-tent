"""LLM conduit (LiteLLM) with provider failover + JSON repair. See spec.

Choice (documented per spec): LiteLLM 1.81 is the conduit — ``acompletion``
for chat-completions formats and ``aresponses`` for OpenAI Responses-API
formats, both against custom ``api_base``. Every request carries the
configured User-Agent and stable session header (OpenCode Go requirements).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import litellm

from circus_tent.config.loader import ModelsConfig, ProviderConfig, RoleConfig
from circus_tent.engine.budgets import BudgetExceeded, BudgetManager
from circus_tent.telemetry import Metrics

_MAX_HEAL_DOM_CHARS = 40_000
_MAX_VISION_IMAGE_EDGE = 1280


class ModelError(Exception):
    """All providers failed, or the response could not be parsed."""


@dataclass(frozen=True)
class HealResponse:
    selector: str | None
    strategy: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class VisionResponse:
    x: int | None
    y: int | None
    width: int | None
    height: int | None
    confidence: float


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[truncated by circus-tent]...\n" + text[-half:]


class ModelClient:
    """The ONLY module that makes LLM calls."""

    def __init__(
        self,
        cfg: ModelsConfig,
        metrics: Metrics,
        budgets: BudgetManager,
        logger: logging.Logger,
    ) -> None:
        self.cfg = cfg
        self.metrics = metrics
        self.budgets = budgets
        self.logger = logger
        self._session_key: str | None = None
        self._shard = "unknown"

    def set_run_context(self, session_key: str | None, shard: str) -> None:
        """Budget-scoping context, set by the engine before each run."""
        self._session_key = session_key
        self._shard = shard

    def build_headers(self, run_id: str) -> dict[str, str]:
        return {
            "User-Agent": self.cfg.user_agent,
            self.cfg.session_header_name: run_id,
        }

    def _resolve_key(self, provider: ProviderConfig) -> str:
        import os

        value = os.environ.get(provider.api_key_env, "")
        if not value:
            raise ModelError(
                f"missing env var {provider.api_key_env!r} for provider {provider.name}"
            )
        return value

    def _charge(self, role: str, provider: ProviderConfig, tokens_in: int, tokens_out: int) -> None:
        try:
            self.budgets.check_and_charge(
                role, self._session_key or "unknown", self._shard, tokens_in, tokens_out
            )
        except BudgetExceeded as e:
            self.logger.warning(
                "llm budget exceeded", extra={"event": "budget_exceeded", "error": str(e)}
            )
            raise ModelError(str(e)) from e
        self.metrics.tokens_consumed.labels(shard=self._shard, role=role, model=provider.name).inc(
            tokens_in + tokens_out
        )

    async def _call_providers(
        self,
        role: RoleConfig,
        payload_fn: Any,
        tokens_in: int,
        run_id: str,
        role_name: str,
    ) -> Any:
        """Try providers in order; timeout/5xx → next; all failed → ModelError."""
        headers = self.build_headers(run_id)
        last_err: Exception | None = None
        for provider in role.providers:
            key = self._resolve_key(provider)
            try:
                result = await payload_fn(provider, key, headers)
                usage = self._usage(provider, result)
                self._charge(role_name, provider, tokens_in, max(usage["out"], 16))
                return result
            except BudgetExceeded:
                raise
            except litellm.exceptions.Timeout as e:
                last_err = e
            except litellm.exceptions.APIConnectionError as e:
                last_err = e
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status is not None and not (500 <= int(status) < 600):
                    raise
                # typed 5xx OR untyped provider error: fail over to the next
                # provider; the last provider's error surfaces as ModelError.
                last_err = e
                continue
        raise ModelError(f"all providers failed for role: {last_err}") from last_err

    def _usage(self, provider: ProviderConfig, result: Any) -> dict[str, int]:
        try:
            usage = getattr(result, "usage", None)
            if usage is None:
                return {"out": 16}
            if provider.api_format == "responses":
                return {
                    "in": int(getattr(usage, "input_tokens", 0)),
                    "out": int(getattr(usage, "output_tokens", 16)),
                }
            return {
                "in": int(getattr(usage, "prompt_tokens", 0)),
                "out": int(getattr(usage, "completion_tokens", 16)),
            }
        except Exception:  # noqa: BLE001
            return {"out": 16}

    async def heal(self, pruned_dom: str, fallback_text: str, failed_selector: str) -> HealResponse:
        dom = _truncate_middle(pruned_dom, _MAX_HEAL_DOM_CHARS)
        prompt = self.cfg.heal_prompt.format(
            fallback_text=fallback_text,
            failed_selector=failed_selector,
            pruned_dom=dom,
        )
        run_id = str(uuid.uuid4())
        role = self.cfg.text_healing
        tokens_in = len(prompt) // 4  # crude pre-call estimate; reconciled post-call

        async def payload_fn(provider: ProviderConfig, key: str, headers: dict[str, str]) -> Any:
            return await litellm.aresponses(
                model=provider.name,
                input=prompt,
                api_base=provider.api_base,
                api_key=key,
                max_output_tokens=max(provider.max_tokens, 16),
                temperature=provider.temperature,
                timeout=provider.timeout_seconds,
                extra_headers=headers,
            )

        raw = await self._call_providers(role, payload_fn, tokens_in, run_id, "text")
        text = getattr(raw, "output_text", "") or ""
        parsed = self._parse_json(text)
        if parsed is None:
            # one retry with an explicit JSON-only instruction
            retry_prompt = prompt + "\n\nReturn ONLY valid JSON. No prose, no code fences."
            raw = await self._call_providers(
                role,
                lambda p, k, h: litellm.aresponses(
                    model=p.name,
                    input=retry_prompt,
                    api_base=p.api_base,
                    api_key=k,
                    max_output_tokens=max(p.max_tokens, 16),
                    temperature=p.temperature,
                    timeout=p.timeout_seconds,
                    extra_headers=h,
                ),
                tokens_in,
                run_id,
                "text",
            )
            text = getattr(raw, "output_text", "") or ""
            parsed = self._parse_json(text)
        if parsed is None:
            raise ModelError("heal response was not parseable JSON")
        return HealResponse(
            selector=parsed.get("selector"),
            strategy=parsed.get("strategy"),
            confidence=float(parsed.get("confidence") or 0.0),
            reason=str(parsed.get("reason") or ""),
        )

    async def locate(self, image_b64: str, fallback_text: str) -> VisionResponse:
        prompt = self.cfg.vision_prompt.format(fallback_text=fallback_text)
        run_id = str(uuid.uuid4())
        role = self.cfg.vision_fallback
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ]
        tokens_in = len(prompt) // 4

        async def payload_fn(provider: ProviderConfig, key: str, headers: dict[str, str]) -> Any:
            return await litellm.acompletion(
                model=provider.name,
                messages=messages,
                api_base=provider.api_base,
                api_key=key,
                max_tokens=provider.max_tokens,
                temperature=provider.temperature,
                timeout=provider.timeout_seconds,
                extra_headers=headers,
            )

        raw = await self._call_providers(role, payload_fn, tokens_in, run_id, "vision")
        text = (raw.choices[0].message.content or "") if raw.choices else ""
        parsed = self._parse_json(text)
        if parsed is None:
            raise ModelError("vision response was not parseable JSON")

        def _int_or_none(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except TypeError, ValueError:
                return None

        return VisionResponse(
            x=_int_or_none(parsed.get("x")),
            y=_int_or_none(parsed.get("y")),
            width=_int_or_none(parsed.get("width")),
            height=_int_or_none(parsed.get("height")),
            confidence=float(parsed.get("confidence") or 0.0),
        )

    def repair_json(self, raw: str) -> dict[str, Any] | None:
        return self._parse_json(raw)

    def _parse_json(self, raw: str) -> dict[str, Any] | None:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        try:
            from llm_json_repair import repair_json as _repair

            fixed = _repair(text)
            parsed = json.loads(fixed)
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            pass
        try:
            import partial_json_parser

            parsed_any: Any = partial_json_parser.loads(text)
            return parsed_any if isinstance(parsed_any, dict) else None
        except Exception:  # noqa: BLE001
            return None
