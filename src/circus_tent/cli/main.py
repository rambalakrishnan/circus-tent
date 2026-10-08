"""CLI entry point. See modules/cli/.omp-spec.md."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="circus-tent")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API server (REST + MCP)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    serve.add_argument("--warmup", action="store_true")
    serve.add_argument("--log-level", default="INFO")

    bootstrap = sub.add_parser("bootstrap", help="headful first-run MFA handoff per shard")
    bootstrap.add_argument("--shard", required=True)
    bootstrap.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    bootstrap.add_argument(
        "--reset",
        action="store_true",
        help="purge existing profile + fingerprint manifest (lockout risk)",
    )
    bootstrap.add_argument("--log-level", default="INFO")
    return parser


def _serve(args: argparse.Namespace) -> int:
    from circus_tent.api.app import create_app, drain_status
    from circus_tent.config.loader import load_env_file
    from circus_tent.telemetry import setup_logging

    load_env_file(REPO_ROOT / ".env")
    setup_logging(args.log_level)
    app = create_app(args.config_dir, warmup=args.warmup)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return drain_status


def _bootstrap(args: argparse.Namespace) -> int:
    from circus_tent.config.loader import load_env_file
    from circus_tent.telemetry import get_logger, setup_logging

    load_env_file(REPO_ROOT / ".env")
    setup_logging(args.log_level)
    logger = get_logger("circus_tent.bootstrap")

    from circus_tent.cli.bootstrap import bootstrap_shard

    return asyncio.run(bootstrap_shard(args.shard, args.config_dir, logger, reset=args.reset))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "bootstrap":
            return _bootstrap(args)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"circus-tent: fatal: {e}", file=sys.stderr)
        return 1
