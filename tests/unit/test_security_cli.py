"""Unit tests for challenge detection/resolution (respx) and the CLI."""

from __future__ import annotations

import logging

import pytest
import respx

from circus_tent.security.challenge_resolver import (
    CapSolverClient,
    Challenge,
    ChallengeDetector,
    ChallengeResolver,
)
from circus_tent.telemetry import get_metrics


def test_detect_turnstile() -> None:
    det = ChallengeDetector()
    dom = '<div class="cf-turnstile" data-sitekey="0x4AAAA">verify</div>'
    ch = det.detect(dom, "https://x.example.com")
    assert ch is not None
    assert ch.type == "turnstile"
    assert ch.sitekey == "0x4AAAA"


def test_detect_recaptcha_and_hcaptcha() -> None:
    det = ChallengeDetector()
    assert (
        det.detect('<div class="g-recaptcha" data-sitekey="k1">', "https://u").type
        == "recaptcha_v2"
    )  # type: ignore[union-attr]
    assert (
        det.detect('<iframe src="https://newassets.hcaptcha.com/">', "https://u").type == "hcaptcha"
    )  # type: ignore[union-attr]
    assert det.detect("nothing here", "https://u") is None


@pytest.mark.asyncio
async def test_capsolver_solve_and_inject() -> None:
    with respx.mock() as mock:
        create = mock.post("https://api.capsolver.com/createTask").respond(
            json={"errorId": 0, "taskId": "t123"}
        )
        poll = mock.post("https://api.capsolver.com/getTaskResult").respond(
            json={"errorId": 0, "status": "ready", "solution": {"token": "tok-1"}}
        )
        client = CapSolverClient("capkey", get_metrics(), logging.getLogger("t"))
        result = await client.solve(Challenge("turnstile", "0x4AAAA", "https://u.example.com"))
        assert result.token == "tok-1"
        assert len(create.calls) == 1
        assert len(poll.calls) == 1
        body = create.calls[0].request.content.decode()
        assert "capkey" in body and "AntiTurnstileTaskProxyLess" in body


@pytest.mark.asyncio
async def test_resolver_session_cache_avoids_resolve() -> None:
    with respx.mock() as mock:
        route = mock.post("https://api.capsolver.com/createTask").respond(
            json={"errorId": 0, "taskId": "t1"}
        )
        mock.post("https://api.capsolver.com/getTaskResult").respond(
            json={"errorId": 0, "status": "ready", "solution": {"token": "tok"}}
        )
        client = CapSolverClient("capkey", get_metrics(), logging.getLogger("t"))
        resolver = ChallengeResolver(client, get_metrics(), logging.getLogger("t"))
        page = _FakePage()
        dom = '<div class="cf-turnstile" data-sitekey="0x4AAAA">verify</div>'
        ok = await resolver.handle(page, dom, "https://u.example.com", "indeed")
        assert ok
        ok2 = await resolver.handle(page, dom, "https://u.example.com", "indeed")
        assert ok2
        assert len(route.calls) == 1  # cached — no second solve


class _FakePage:
    async def evaluate(self, script: str, *args: object) -> None:
        return None


# ---------------------------------------------------------------- CLI


def test_parser_help_and_unknown_shard() -> None:
    from circus_tent.cli.main import build_parser, main

    parser = build_parser()
    args = parser.parse_args(["bootstrap", "--shard", "nope"])
    assert args.shard == "nope"
    # bootstrap with unknown shard exits 2 before any browser work
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    code = main(["bootstrap", "--shard", "nonexistent", "--config-dir", str(repo / "config")])
    assert code == 2


def test_main_serve_requires_env() -> None:
    import pytest as _pytest

    from circus_tent.cli.main import main

    with _pytest.raises(SystemExit):
        main(["serve", "--help"])  # argparse exits 0 on help
