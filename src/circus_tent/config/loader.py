"""Config loading with env-var references. See modules/config/.omp-spec.md."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised for any configuration problem (missing file, bad schema, bad ref)."""


_ENV_REF = re.compile(r"\$\{([^{}]*)\}")
_MAX_ENV_NESTING = 10


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from `path` into os.environ ONLY for keys not already set.

    Dev fallback for the repo-root .env; never overrides real env vars.
    """
    env_path = Path(path)
    if not env_path.is_file():
        raise ConfigError(f"env file not found: {env_path}")
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"could not read env file {env_path}: {exc}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class BackoffConfig:
    triggers: tuple[int, ...]
    initial_wait_seconds: float
    max_wait_seconds: float
    cooldown_seconds: float


@dataclass(frozen=True)
class ShardConfig:
    name: str
    domain_patterns: tuple[str, ...]
    profile_dir: str
    max_tabs: int
    recycle_after_pages: int
    recycle_grace_seconds: float
    headless: str
    request_rate_per_minute: float
    session_step_rate_per_minute: float
    backoff: BackoffConfig
    preflight_url: str
    preflight_marker: str


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_base: str
    api_key_env: str
    api_format: str
    max_tokens: int
    temperature: float
    timeout_seconds: float


@dataclass(frozen=True)
class RoleConfig:
    providers: tuple[ProviderConfig, ...]


@dataclass(frozen=True)
class BudgetsConfig:
    session_daily_text_tokens: int
    session_daily_vision_tokens: int
    shard_daily_text_tokens: int
    shard_daily_vision_tokens: int
    cb_window_seconds: float
    cb_heal_rate_threshold: float
    cb_cooldown_seconds: float


@dataclass(frozen=True)
class ModelsConfig:
    text_healing: RoleConfig
    vision_fallback: RoleConfig
    budgets: BudgetsConfig
    user_agent: str
    session_header_name: str
    prompt_version: int
    heal_prompt: str
    vision_prompt: str


@dataclass(frozen=True)
class AccountConfig:
    id: str
    shard: str
    username_env: str
    password_env: str
    tenant: str | None
    mfa_profile: str
    quota: int


@dataclass(frozen=True)
class CallerConfig:
    id: str
    key_env: str
    shards: tuple[str, ...]
    tools: tuple[str, ...]
    accounts: tuple[str, ...]
    rate_limit_per_minute: float


_SHARDS_TOP_KEYS = {"schema_version", "profiles_root", "defaults", "shards"}
_SHARDS_DEFAULTS_KEYS = {
    "max_tabs",
    "recycle_after_pages",
    "recycle_grace_seconds",
    "headless",
    "request_rate_per_minute",
    "session_step_rate_per_minute",
    "backoff",
}
_SHARD_KEYS = _SHARDS_DEFAULTS_KEYS | {"name", "domain_patterns", "profile_dir", "preflight"}
_BACKOFF_KEYS = {
    "triggers",
    "initial_wait_seconds",
    "max_wait_seconds",
    "cooldown_seconds",
}
_PREFLIGHT_KEYS = {"url", "marker"}
_HEADLESS_VALUES = {"virtual", "true", "false"}

_MODELS_TOP_KEYS = {
    "schema_version",
    "roles",
    "budgets",
    "client_headers",
    "prompt_templates",
}
_MODEL_ROLES_KEYS = {"text_healing", "vision_fallback"}
_ROLE_KEYS = {"providers"}
_PROVIDER_KEYS = {
    "name",
    "api_base",
    "api_key_env",
    "api_format",
    "max_tokens",
    "temperature",
    "timeout_seconds",
}
_API_FORMATS = {"responses", "chat_completions"}
_BUDGETS_KEYS = {
    "session_daily_text_tokens",
    "session_daily_vision_tokens",
    "shard_daily_text_tokens",
    "shard_daily_vision_tokens",
    "circuit_breaker",
}
_CIRCUIT_BREAKER_KEYS = {"window_seconds", "heal_rate_threshold", "cooldown_seconds"}
_CLIENT_HEADERS_KEYS = {"user_agent", "session_header_name"}
_PROMPT_TEMPLATES_KEYS = {"version", "heal", "vision"}

_ACCOUNTS_TOP_KEYS = {"schema_version", "accounts"}
_ACCOUNT_KEYS = {
    "id",
    "shard",
    "username_env",
    "password_env",
    "tenant",
    "mfa_profile",
    "quota",
}

_CALLERS_TOP_KEYS = {"schema_version", "callers"}
_CALLER_KEYS = {"id", "key_env", "shards", "tools", "accounts", "rate_limit_per_minute"}


def _require_str(node: Any, where: str) -> str:
    if not isinstance(node, str):
        raise ConfigError(f"{where}: expected a string, got {node!r}")
    return node


def _require_int(node: Any, where: str) -> int:
    if isinstance(node, bool) or not isinstance(node, int):
        raise ConfigError(f"{where}: expected an integer, got {node!r}")
    return node


def _require_number(node: Any, where: str) -> float:
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {node!r}")
    return float(node)


def _require_list(node: Any, where: str) -> list[Any]:
    if not isinstance(node, list):
        raise ConfigError(f"{where}: expected a list, got {type(node).__name__}")
    return node


def _pick(entry: dict[Any, Any], defaults: dict[Any, Any], key: str, where: str) -> Any:
    """Shard field resolution: per-shard value, else shared default, else error."""
    if key in entry and entry[key] is not None:
        return entry[key]
    if key in defaults:
        return defaults[key]
    raise ConfigError(f"{where}: required key {key!r} missing")


class ConfigLoader:
    """Loads config/*.yaml and resolves ${VAR} / ${VAR:-default} references."""

    def __init__(self, config_dir: Path | str, env: Mapping[str, str] | None = None) -> None:
        self.config_dir = Path(config_dir)
        self.env = dict(os.environ if env is None else env)

    # ------------------------------------------------------------------ helpers

    def _check_keys(self, node: Any, allowed: set[str], where: str) -> dict[Any, Any]:
        if not isinstance(node, dict):
            raise ConfigError(f"{where}: expected a mapping, got {type(node).__name__}")
        unknown = set(node) - allowed
        if unknown:
            raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
        return node

    def _check_version(self, data: dict[Any, Any], file_name: str) -> None:
        version = data.get("schema_version")
        if version != 1:
            raise ConfigError(f"{file_name}: unsupported schema_version {version!r} (expected 1)")

    def _load_yaml(self, file_name: str) -> dict[Any, Any]:
        path = self.config_dir / file_name
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"could not read config file {path}: {exc}") from exc
        try:
            data: Any = yaml.safe_load(raw)  # Untyped YAML; validated via strict key checks.
        except yaml.YAMLError as exc:
            raise ConfigError(f"{file_name}: invalid YAML: {exc}") from exc
        if data is None:
            raise ConfigError(f"{file_name}: config file is empty")
        if not isinstance(data, dict):
            raise ConfigError(f"{file_name}: expected a top-level mapping")
        return data

    def _home_dir(self) -> Path:
        home = self.env.get("HOME") or os.environ.get("HOME")
        if home:
            return Path(home)
        return Path.home()

    def _expand_path(self, raw: str) -> str:
        """Resolve env refs and expand a leading ``~`` (or bare ``~``) to HOME."""
        expanded = self.resolve_env(raw)
        if expanded == "~":
            return str(self._home_dir())
        if expanded.startswith("~/"):
            home = self._home_dir()
            return str(home / expanded[2:])
        return expanded

    # ------------------------------------------------------------- public API

    def resolve_env(self, value: str) -> str:
        """Expand ``${VAR}`` / ``${VAR:-default}`` references.

        ``${VAR}`` raises ConfigError when VAR is not set; ``${VAR:-default}``
        falls back to the default when VAR is unset or empty.
        """
        result = value
        for _ in range(_MAX_ENV_NESTING):
            match = _ENV_REF.search(result)
            if match is None:
                return result
            inner = match.group(1)
            if ":-" in inner:
                name, default = inner.split(":-", 1)
                raw = self.env.get(name)
                replacement = default if raw is None or raw == "" else raw
            else:
                name = inner
                if name not in self.env:
                    raise ConfigError(f"environment variable {name!r} referenced but not set")
                replacement = self.env[name]
            result = result[: match.start()] + replacement + result[match.end() :]
        raise ConfigError(f"too many nested environment references in {value!r}")

    def resolve_secret(self, env_name: str) -> str:
        """Return the value of env var `env_name`; ConfigError if unset or empty."""
        raw = self.env.get(env_name)
        if raw is None or not raw.strip():
            raise ConfigError(f"secret environment variable {env_name!r} is unset or empty")
        return raw

    def load_shards(self) -> tuple[ShardConfig, ...]:
        data = self._load_yaml("shards.yaml")
        self._check_keys(data, _SHARDS_TOP_KEYS, "shards.yaml")
        self._check_version(data, "shards.yaml")

        profiles_root_raw = data.get("profiles_root")
        root_where = "shards.yaml:profiles_root"
        if profiles_root_raw is None or not _require_str(profiles_root_raw, root_where).strip():
            raise ConfigError("shards.yaml: 'profiles_root' is required and must not be empty")
        profiles_root = Path(self._expand_path(_require_str(profiles_root_raw, root_where)))
        if not profiles_root.is_absolute():
            profiles_root = (Path.cwd() / profiles_root).absolute()
        # Shard profile_dir values are relative to the profiles base dir
        # (the parent of profiles_root, e.g. ${PROFILES_DIR} itself).
        base_dir = profiles_root.parent

        defaults_raw = data.get("defaults")
        defaults = (
            {}
            if defaults_raw is None
            else self._check_keys(defaults_raw, _SHARDS_DEFAULTS_KEYS, "shards.yaml:defaults")
        )

        shards_raw = data.get("shards")
        if not isinstance(shards_raw, list) or not shards_raw:
            raise ConfigError("shards.yaml: 'shards' must be a non-empty list")

        shards: list[ShardConfig] = []
        seen_names: set[str] = set()
        for index, entry in enumerate(shards_raw):
            where = f"shards.yaml:shards[{index}]"
            entry_dict = self._check_keys(entry, _SHARD_KEYS, where)

            name = _require_str(entry_dict.get("name"), f"{where}:name").strip()
            if not name:
                raise ConfigError(f"{where}: 'name' must not be empty")
            if name in seen_names:
                raise ConfigError(f"shards.yaml: duplicate shard name {name!r}")
            seen_names.add(name)

            patterns_raw = entry_dict.get("domain_patterns")
            if not isinstance(patterns_raw, list) or not patterns_raw:
                raise ConfigError(f"{where}: 'domain_patterns' must be a non-empty list")
            patterns = tuple(
                _require_str(p, f"{where}:domain_patterns").strip() for p in patterns_raw
            )
            if any(not p for p in patterns):
                raise ConfigError(f"{where}: domain patterns must not be empty strings")

            profile_dir_raw = entry_dict.get("profile_dir")
            dir_where = f"{where}:profile_dir"
            if profile_dir_raw is None or not _require_str(profile_dir_raw, dir_where).strip():
                raise ConfigError(f"{where}: 'profile_dir' is required and must not be empty")
            profile_dir = Path(self._expand_path(_require_str(profile_dir_raw, dir_where)))
            if not profile_dir.is_absolute():
                profile_dir = base_dir / profile_dir
            profile_dir = profile_dir.absolute()

            max_tabs = _require_int(
                _pick(entry_dict, defaults, "max_tabs", where), f"{where}:max_tabs"
            )
            recycle_after_pages = _require_int(
                _pick(entry_dict, defaults, "recycle_after_pages", where),
                f"{where}:recycle_after_pages",
            )
            recycle_grace_seconds = _require_number(
                _pick(entry_dict, defaults, "recycle_grace_seconds", where),
                f"{where}:recycle_grace_seconds",
            )
            headless = _require_str(
                _pick(entry_dict, defaults, "headless", where), f"{where}:headless"
            )
            if headless not in _HEADLESS_VALUES:
                raise ConfigError(
                    f"{where}: 'headless' must be one of virtual|true|false, got {headless!r}"
                )
            request_rate = _require_number(
                _pick(entry_dict, defaults, "request_rate_per_minute", where),
                f"{where}:request_rate_per_minute",
            )
            session_step_rate = _require_number(
                _pick(entry_dict, defaults, "session_step_rate_per_minute", where),
                f"{where}:session_step_rate_per_minute",
            )

            backoff_raw = entry_dict.get("backoff")
            if backoff_raw is None and "backoff" in defaults:
                backoff_raw = defaults["backoff"]
            if backoff_raw is None:
                raise ConfigError(f"{where}: required key 'backoff' missing")
            backoff_dict = self._check_keys(backoff_raw, _BACKOFF_KEYS, f"{where}:backoff")
            triggers_raw = backoff_dict.get("triggers")
            if not isinstance(triggers_raw, list):
                raise ConfigError(f"{where}:backoff: 'triggers' must be a list")
            triggers = tuple(_require_int(t, f"{where}:backoff:triggers") for t in triggers_raw)
            backoff = BackoffConfig(
                triggers=triggers,
                initial_wait_seconds=_require_number(
                    backoff_dict.get("initial_wait_seconds"),
                    f"{where}:backoff:initial_wait_seconds",
                ),
                max_wait_seconds=_require_number(
                    backoff_dict.get("max_wait_seconds"), f"{where}:backoff:max_wait_seconds"
                ),
                cooldown_seconds=_require_number(
                    backoff_dict.get("cooldown_seconds"), f"{where}:backoff:cooldown_seconds"
                ),
            )

            preflight_raw = entry_dict.get("preflight")
            if preflight_raw is None:
                preflight_url, preflight_marker = "", ""
            else:
                preflight = self._check_keys(preflight_raw, _PREFLIGHT_KEYS, f"{where}:preflight")
                preflight_url = self.resolve_env(
                    _require_str(preflight.get("url", ""), f"{where}:preflight:url")
                )
                preflight_marker = _require_str(
                    preflight.get("marker", ""), f"{where}:preflight:marker"
                )

            shards.append(
                ShardConfig(
                    name=name,
                    domain_patterns=patterns,
                    profile_dir=str(profile_dir),
                    max_tabs=max_tabs,
                    recycle_after_pages=recycle_after_pages,
                    recycle_grace_seconds=recycle_grace_seconds,
                    headless=headless,
                    request_rate_per_minute=request_rate,
                    session_step_rate_per_minute=session_step_rate,
                    backoff=backoff,
                    preflight_url=preflight_url,
                    preflight_marker=preflight_marker,
                )
            )

        last = shards[-1]
        if last.name != "misc" or "*" not in last.domain_patterns:
            raise ConfigError("shards.yaml: the last shard must be 'misc' with domain pattern '*'")
        return tuple(shards)

    def load_models(self) -> ModelsConfig:
        data = self._load_yaml("models.yaml")
        self._check_keys(data, _MODELS_TOP_KEYS, "models.yaml")
        self._check_version(data, "models.yaml")

        roles_raw = self._check_keys(data.get("roles"), _MODEL_ROLES_KEYS, "models.yaml:roles")

        def parse_role(role_name: str) -> RoleConfig:
            role_where = f"models.yaml:roles:{role_name}"
            role = self._check_keys(roles_raw.get(role_name), _ROLE_KEYS, role_where)
            providers_raw = role.get("providers")
            if not isinstance(providers_raw, list) or not providers_raw:
                raise ConfigError(f"{role_where}: 'providers' must be a non-empty list")
            providers: list[ProviderConfig] = []
            for index, raw in enumerate(providers_raw):
                where = f"{role_where}:providers[{index}]"
                provider = self._check_keys(raw, _PROVIDER_KEYS, where)
                api_format = _require_str(provider.get("api_format"), f"{where}:api_format")
                if api_format not in _API_FORMATS:
                    raise ConfigError(
                        f"{where}: 'api_format' must be one of responses|chat_completions, "
                        f"got {api_format!r}"
                    )
                providers.append(
                    ProviderConfig(
                        name=_require_str(provider.get("name"), f"{where}:name"),
                        api_base=self.resolve_env(
                            _require_str(provider.get("api_base"), f"{where}:api_base")
                        ),
                        api_key_env=_require_str(
                            provider.get("api_key_env"), f"{where}:api_key_env"
                        ),
                        api_format=api_format,
                        max_tokens=_require_int(provider.get("max_tokens"), f"{where}:max_tokens"),
                        temperature=_require_number(
                            provider.get("temperature"), f"{where}:temperature"
                        ),
                        timeout_seconds=_require_number(
                            provider.get("timeout_seconds"), f"{where}:timeout_seconds"
                        ),
                    )
                )
            return RoleConfig(providers=tuple(providers))

        budgets_raw = self._check_keys(data.get("budgets"), _BUDGETS_KEYS, "models.yaml:budgets")
        cb_where = "models.yaml:budgets:circuit_breaker"
        cb_raw = self._check_keys(
            budgets_raw.get("circuit_breaker"), _CIRCUIT_BREAKER_KEYS, cb_where
        )
        budgets = BudgetsConfig(
            session_daily_text_tokens=_require_int(
                budgets_raw.get("session_daily_text_tokens"),
                "models.yaml:budgets:session_daily_text_tokens",
            ),
            session_daily_vision_tokens=_require_int(
                budgets_raw.get("session_daily_vision_tokens"),
                "models.yaml:budgets:session_daily_vision_tokens",
            ),
            shard_daily_text_tokens=_require_int(
                budgets_raw.get("shard_daily_text_tokens"),
                "models.yaml:budgets:shard_daily_text_tokens",
            ),
            shard_daily_vision_tokens=_require_int(
                budgets_raw.get("shard_daily_vision_tokens"),
                "models.yaml:budgets:shard_daily_vision_tokens",
            ),
            cb_window_seconds=_require_number(
                cb_raw.get("window_seconds"), f"{cb_where}:window_seconds"
            ),
            cb_heal_rate_threshold=_require_number(
                cb_raw.get("heal_rate_threshold"), f"{cb_where}:heal_rate_threshold"
            ),
            cb_cooldown_seconds=_require_number(
                cb_raw.get("cooldown_seconds"), f"{cb_where}:cooldown_seconds"
            ),
        )

        headers_raw = self._check_keys(
            data.get("client_headers"), _CLIENT_HEADERS_KEYS, "models.yaml:client_headers"
        )
        prompts_raw = self._check_keys(
            data.get("prompt_templates"), _PROMPT_TEMPLATES_KEYS, "models.yaml:prompt_templates"
        )

        return ModelsConfig(
            text_healing=parse_role("text_healing"),
            vision_fallback=parse_role("vision_fallback"),
            budgets=budgets,
            user_agent=_require_str(
                headers_raw.get("user_agent"), "models.yaml:client_headers:user_agent"
            ),
            session_header_name=_require_str(
                headers_raw.get("session_header_name"),
                "models.yaml:client_headers:session_header_name",
            ),
            prompt_version=_require_int(
                prompts_raw.get("version"), "models.yaml:prompt_templates:version"
            ),
            heal_prompt=_require_str(prompts_raw.get("heal"), "models.yaml:prompt_templates:heal"),
            vision_prompt=_require_str(
                prompts_raw.get("vision"), "models.yaml:prompt_templates:vision"
            ),
        )

    def load_accounts(self) -> tuple[AccountConfig, ...]:
        data = self._load_yaml("accounts.yaml")
        self._check_keys(data, _ACCOUNTS_TOP_KEYS, "accounts.yaml")
        self._check_version(data, "accounts.yaml")

        accounts_raw = data.get("accounts")
        if not isinstance(accounts_raw, list):
            raise ConfigError("accounts.yaml: 'accounts' must be a list")

        accounts: list[AccountConfig] = []
        for index, raw in enumerate(accounts_raw):
            where = f"accounts.yaml:accounts[{index}]"
            account = self._check_keys(raw, _ACCOUNT_KEYS, where)
            tenant_raw = account.get("tenant")
            tenant = _require_str(tenant_raw, f"{where}:tenant") if tenant_raw is not None else None
            quota = _require_int(account.get("quota"), f"{where}:quota")
            if quota <= 0:
                raise ConfigError(f"{where}: 'quota' must be greater than 0, got {quota}")
            accounts.append(
                AccountConfig(
                    id=_require_str(account.get("id"), f"{where}:id"),
                    shard=_require_str(account.get("shard"), f"{where}:shard"),
                    username_env=_require_str(account.get("username_env"), f"{where}:username_env"),
                    password_env=_require_str(account.get("password_env"), f"{where}:password_env"),
                    tenant=tenant,
                    mfa_profile=_require_str(account.get("mfa_profile"), f"{where}:mfa_profile"),
                    quota=quota,
                )
            )
        return tuple(accounts)

    def load_callers(self) -> tuple[CallerConfig, ...]:
        data = self._load_yaml("callers.yaml")
        self._check_keys(data, _CALLERS_TOP_KEYS, "callers.yaml")
        self._check_version(data, "callers.yaml")

        callers_raw = data.get("callers")
        if not isinstance(callers_raw, list):
            raise ConfigError("callers.yaml: 'callers' must be a list")

        callers: list[CallerConfig] = []
        for index, raw in enumerate(callers_raw):
            where = f"callers.yaml:callers[{index}]"
            caller = self._check_keys(raw, _CALLER_KEYS, where)
            callers.append(
                CallerConfig(
                    id=_require_str(caller.get("id"), f"{where}:id"),
                    key_env=_require_str(caller.get("key_env"), f"{where}:key_env"),
                    shards=tuple(
                        _require_str(item, f"{where}:shards")
                        for item in _require_list(caller.get("shards"), f"{where}:shards")
                    ),
                    tools=tuple(
                        _require_str(item, f"{where}:tools")
                        for item in _require_list(caller.get("tools"), f"{where}:tools")
                    ),
                    accounts=tuple(
                        _require_str(item, f"{where}:accounts")
                        for item in _require_list(caller.get("accounts"), f"{where}:accounts")
                    ),
                    rate_limit_per_minute=_require_number(
                        caller.get("rate_limit_per_minute"), f"{where}:rate_limit_per_minute"
                    ),
                )
            )
        return tuple(callers)

    def shard_for_domain(self, domain: str, shards: Sequence[ShardConfig]) -> ShardConfig:
        """Return the first shard whose pattern matches `domain` in declaration order.

        Pattern semantics: bare ``*`` matches everything; ``*.suffix`` matches
        strict subdomains of ``suffix`` (not the apex); anything else must match
        exactly. Domain comparison is case-insensitive.
        """
        if not shards:
            raise ConfigError("no shards configured to route domain")
        normalized = domain.strip().lower()
        for shard in shards:
            for pattern in shard.domain_patterns:
                if self._pattern_matches(normalized, pattern):
                    return shard
        raise ConfigError(f"no shard matches domain {domain!r}")

    @staticmethod
    def _pattern_matches(domain: str, pattern: str) -> bool:
        lowered = pattern.strip().lower()
        if lowered == "*":
            return True
        if lowered.startswith("*."):
            suffix = lowered[2:]
            return domain.endswith("." + suffix)
        return domain == lowered
