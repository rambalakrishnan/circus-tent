"""Unit tests for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from circus_tent.config.loader import ConfigError, ConfigLoader, load_env_file

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def loader_env() -> dict:
    import os

    return {
        **os.environ,
        "MUSE_API_BASE": "https://example.com/responses",
        "MUSE_API_KEY": "k-muse",
        "DEEPSEEK_API_BASE": "https://example.com/chat",
        "DEEPSEEK_API_KEY": "k-deepseek",
        "CAPSOLVER_API_KEY": "k-cap",
        "AUTOMATION_WRAPPER_API_KEY": "k-api",
        "PROFILES_DIR": "/tmp/ct-test-profiles",
        "NEOGOV_USERNAME": "u",
        "NEOGOV_PASSWORD": "p",
        "LINKEDIN_USERNAME": "u",
        "LINKEDIN_PASSWORD": "p",
        "WORKDAY_ACME_USERNAME": "u",
        "WORKDAY_ACME_PASSWORD": "p",
    }


def test_resolve_env_plain_and_default(loader_env: dict) -> None:
    loader = ConfigLoader(REPO / "config", env=loader_env)
    assert loader.resolve_env("${MUSE_API_BASE}") == "https://example.com/responses"
    assert loader.resolve_env("${NOPE:-fallback}") == "fallback"
    with pytest.raises(ConfigError):
        loader.resolve_env("${NOPE}")


def test_resolve_secret_missing_raises(loader_env: dict) -> None:
    loader = ConfigLoader(REPO / "config", env=loader_env)
    assert loader.resolve_secret("MUSE_API_KEY") == "k-muse"
    with pytest.raises(ConfigError):
        loader.resolve_secret("DOES_NOT_EXIST_ANYWHERE")


def test_load_shards_routing(loader_env: dict) -> None:
    loader = ConfigLoader(REPO / "config", env=loader_env)
    shards = loader.load_shards()
    names = [s.name for s in shards]
    assert names[-1] == "misc"
    workday = loader.shard_for_domain("nvidia.wd5.myworkdayjobs.com", shards)
    assert workday.name == "workday"
    assert loader.shard_for_domain("boards.greenhouse.io", shards).name == "greenhouse"
    assert loader.shard_for_domain("example.com", shards).name == "misc"
    assert loader.shard_for_domain("www.indeed.com", shards).name == "indeed"
    # apex does not match subdomain-only pattern (strict semantics)
    assert loader.shard_for_domain("workdayjobs.com", shards).name == "misc"
    # real Workday career boards
    assert loader.shard_for_domain("nvidia.wd5.myworkdayjobs.com", shards).name == "workday"
    assert Path(workday.profile_dir).is_absolute()


def test_load_models_accounts_callers(loader_env: dict) -> None:
    loader = ConfigLoader(REPO / "config", env=loader_env)
    models = loader.load_models()
    assert [p.name for p in models.text_healing.providers][0] == "muse-spark-1.3-contributor"
    assert models.prompt_version == 1
    assert "{pruned_dom}" in models.heal_prompt
    accounts = loader.load_accounts()
    assert {a.id for a in accounts} == {"neogov", "linkedin", "workday_acme"}
    callers = loader.load_callers()
    assert callers[0].id == "local-dev"


def test_env_isolation(loader_env: dict) -> None:
    import os

    loader = ConfigLoader(REPO / "config", env=loader_env)
    assert loader.env is not os.environ
    loader.load_shards()


def test_load_env_file_only_fills_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nB=2\n")
    import os

    os.environ["A"] = "existing"
    load_env_file(env_file)
    assert os.environ["A"] == "existing"  # never overrides
    assert os.environ["B"] == "2"
    del os.environ["A"], os.environ["B"]
