"""Unit tests for the LLM conduit — litellm fully mocked, no network."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from circus_tent.config.loader import (
    BudgetsConfig,
    ModelsConfig,
    ProviderConfig,
    RoleConfig,
)
from circus_tent.engine.budgets import BudgetManager
from circus_tent.engine.models import HealResponse, ModelClient, ModelError
from circus_tent.telemetry import get_metrics

CFG = ModelsConfig(
    text_healing=RoleConfig(
        providers=(
            ProviderConfig(
                "muse-spark-1.3-contributor",
                "https://x/responses",
                "MUSE_API_KEY",
                "responses",
                4096,
                0.1,
                30.0,
            ),
            ProviderConfig(
                "muse-spark-1.2-contributor",
                "https://x/responses",
                "MUSE_API_KEY",
                "responses",
                4096,
                0.1,
                30.0,
            ),
        )
    ),
    vision_fallback=RoleConfig(
        providers=(
            ProviderConfig(
                "deepseek-v4-flash-vision-exp",
                "https://x/chat",
                "DEEPSEEK_API_KEY",
                "chat_completions",
                1024,
                0.1,
                30.0,
            ),
        )
    ),
    budgets=BudgetsConfig(10**9, 10**9, 10**9, 10**9, 3600.0, 0.3, 1800.0),
    user_agent="circus-tent/0.1",
    session_header_name="x-opencode-session",
    prompt_version=1,
    heal_prompt="fix {fallback_text} vs {failed_selector}; dom: {pruned_dom}",
    vision_prompt="find {fallback_text}",
)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> ModelClient:
    monkeypatch.setenv("MUSE_API_KEY", "k1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k2")
    budgets = BudgetManager(CFG.budgets, get_metrics(), logging.getLogger("t"), clock=lambda: 0.0)
    return ModelClient(CFG, get_metrics(), budgets, logging.getLogger("t"))


def test_build_headers(client: ModelClient) -> None:
    headers = client.build_headers("run-1")
    assert headers["User-Agent"] == "circus-tent/0.1"
    assert headers["x-opencode-session"] == "run-1"


def test_repair_json_chain(client: ModelClient) -> None:
    assert (
        client.repair_json(
            '{"selector": "a", "strategy": "css", "confidence": 0.9, "reason": "ok"}'
        )
        is not None
    )
    assert client.repair_json("not json at all") is None
    # fenced JSON
    parsed = client.repair_json('```json\n{"selector": "x"}\n```')
    assert parsed == {"selector": "x"}


def test_heal_happy_path(client: ModelClient, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = '{"selector": "#email", "strategy": "css", "confidence": 0.9, "reason": "match"}'
    fake = SimpleNamespace(output_text=payload)

    async def fake_responses(**kwargs):
        return fake

    monkeypatch.setattr("circus_tent.engine.models.litellm.aresponses", fake_responses)
    import asyncio

    resp = asyncio.run(client.heal("<div>x</div>", "Email Address", "[data-x='email']"))
    assert isinstance(resp, HealResponse)
    assert resp.selector == "#email"
    assert resp.strategy == "css"


def test_heal_failover_then_success(client: ModelClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_responses(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Exception("provider down")
        payload = '{"selector": "input.x", "strategy": "css", "confidence": 0.5, "reason": "ok"}'
        return SimpleNamespace(output_text=payload)

    monkeypatch.setattr("circus_tent.engine.models.litellm.aresponses", fake_responses)
    import asyncio

    resp = asyncio.run(client.heal("<div/>", "Name", "[x]"))
    assert resp.selector == "input.x"
    assert len(calls) == 2  # failover to provider 2


def test_heal_unparseable_raises(client: ModelClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_responses(**kwargs):
        return SimpleNamespace(output_text="I cannot help")

    monkeypatch.setattr("circus_tent.engine.models.litellm.aresponses", fake_responses)
    import asyncio

    with pytest.raises(ModelError):
        asyncio.run(client.heal("<div/>", "X", "[x]"))


def test_locate_parses_bbox(client: ModelClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='{"x": 10, "y": 20, "width": 100, "height": 30, "confidence": 0.8}'
                )
            )
        ],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20),
    )
    monkeypatch.setattr(
        "circus_tent.engine.models.litellm.acompletion", AsyncMock(return_value=fake)
    )
    import asyncio

    resp = asyncio.run(client.locate("aGk=", "Submit"))
    assert resp.x == 10 and resp.y == 20 and resp.width == 100 and resp.height == 30
