#!/usr/bin/env python3
"""Manual LLM smoke test — verifies both OpenCode Go endpoints and the ModelClient.

Usage: python3 benchmarks/llm_smoke.py [--via-client]
  Default: raw httpx against the verified endpoint shapes (no circus_tent code).
  --via-client: exercise circus_tent.engine.models.ModelClient (needs implemented
  modules + budgets wiring; models the real call path).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def raw_smoke() -> int:
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("MUSE_API_KEY", "").strip()
    if not key:
        # dev fallback: repo-root .env
        env_path = REPO / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("MUSE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        print("MUSE_API_KEY not set (env or .env)")
        return 2

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "circus-tent/0.1",
        "x-opencode-session": "llm-smoke-001",
    }

    def post(url: str, body: dict) -> tuple[int, str]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()[:200]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:200]

    ok = True
    s1, b1 = post(
        "https://opencode.ai/zen/go/v1/chat/completions",
        {
            "model": "deepseek-v4-flash-vision-exp",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        },
    )
    print("chat/completions:", s1, b1)
    ok &= s1 == 200

    s2, b2 = post(
        "https://opencode.ai/zen/go/v1/responses",
        {"model": "muse-spark-1.3-contributor", "input": "ping", "max_output_tokens": 16},
    )
    print("responses:", s2, b2)
    ok &= s2 == 200
    return 0 if ok else 1


def via_client() -> int:
    from circus_tent.config.loader import ConfigLoader
    from circus_tent.engine.budgets import BudgetManager
    from circus_tent.engine.models import ModelClient
    from circus_tent.telemetry import get_logger, get_metrics

    loader = ConfigLoader(REPO / "config")
    cfg = loader.load_models()
    budgets = BudgetManager(cfg.budgets, get_metrics(), get_logger("llm_smoke"))
    client = ModelClient(cfg, get_metrics(), budgets, get_logger("llm_smoke"))

    import asyncio

    async def main() -> int:
        heal = await client.heal(
            "<html><body><input id='email'></body></html>",
            "Email Address",
            "[data-automation-id='email']",
        )
        print("heal response:", heal)
        return 0 if heal.selector else 1

    return asyncio.run(main())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--via-client", action="store_true")
    args = parser.parse_args()
    raise SystemExit(via_client() if args.via_client else raw_smoke())
