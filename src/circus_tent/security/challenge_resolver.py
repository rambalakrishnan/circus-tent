"""CapSolver integration: detect → solve → inject, with budgets. See spec."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from circus_tent.telemetry import Metrics

CAPSOLVER_BASE_URL = "https://api.capsolver.com"
CREATE_TASK_URL = f"{CAPSOLVER_BASE_URL}/createTask"
GET_TASK_RESULT_URL = f"{CAPSOLVER_BASE_URL}/getTaskResult"

#: Rate-limit retry budget (HTTP 429 or errorId 1), per docs/external/capsolver.md.
MAX_RATE_LIMIT_RETRIES = 3
#: Poll backoff ceiling for both rate-limit retries and status polling.
MAX_BACKOFF_SECONDS = 30.0
#: reCAPTCHA v3: requested minScore in the task; results below the acceptable
#: floor are rejected as failed solves.
RECAPTCHA_V3_REQUESTED_MIN_SCORE = 0.9
RECAPTCHA_V3_ACCEPTABLE_MIN_SCORE = 0.7

#: challenge.type → CapSolver task type (all enabled per operator decision).
TASK_TYPES: dict[str, str] = {
    "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
    "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
    "hcaptcha": "HCaptchaTaskProxyLess",
    "funcaptcha": "FunCaptchaTaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
    "datadome": "DataDomeTask",
    "geetest": "GeeTestTask",
    "imperva": "ImpervaTask",
    "akamai": "AkamaiBMPTask",
    "perimeterx": "PerimeterXTask",
}

#: Challenge types whose solution is a plain token injected into page state.
TOKEN_TYPES = frozenset({"recaptcha_v2", "recaptcha_v3", "hcaptcha", "funcaptcha", "turnstile"})

#: Preferred cookie name carried by each cookie-based challenge's solution.
COOKIE_NAMES: dict[str, str] = {
    "datadome": "datadome",
    "imperva": "reese84",
    "akamai": "_abck",
    "perimeterx": "_px3",
}

_DATA_SITEKEY_RE = re.compile(r"""data-sitekey\s*=\s*["']([^"']+)["']""")
_RECAPTCHA_RENDER_RE = re.compile(r"""recaptcha/api\.js\?render=([A-Za-z0-9_-]{10,})""")
_FUNCAPTCHA_PUBKEY_RE = re.compile(
    r"""(?:data-pkey|data-public-key|public_key)\s*=\s*["']([^"']+)["']"""
)
_GEETEST_GT_RE = re.compile(r"""gt\s*=\s*["']([A-Za-z0-9]{16,})["']""")

#: Ordered DOM signatures per challenge type (matched against lowercased text).
#: Order matters: more distinctive types (v3 markers) are checked before
#: generic fallbacks (v2).
_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("funcaptcha", ("funcaptcha", "arkose", "arkoselabs")),
    ("hcaptcha", ("hcaptcha.com", "h-captcha")),
    ("turnstile", ("cf-turnstile", "challenges.cloudflare.com/turnstile")),
    ("datadome", ("datadome", "captcha-delivery.com")),
    ("geetest", ("geetest",)),
    ("imperva", ("imperva", "incapsula")),
    ("akamai", ("akamai",)),
    ("perimeterx", ("perimeterx", "px-captcha", "_pxcaptcha")),
    (
        "recaptcha_v3",
        ("grecaptcha.execute", "grecaptcha.enterprise.execute", "grecaptcha-badge"),
    ),
    ("recaptcha_v2", ("grecaptcha", "g-recaptcha", "recaptcha")),
)


class CapSolverError(Exception):
    """Solve failed permanently (bad task, unsupported params, auth)."""


@dataclass(frozen=True)
class Challenge:
    type: str
    sitekey: str | None
    url: str


@dataclass(frozen=True)
class SolveResult:
    task_id: str
    token: str | None
    cookie_name: str | None
    cookie_value: str | None


def _utc_day_key() -> str:
    """UTC calendar-day key (YYYY-MM-DD) used for solve budgeting."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _extract_sitekey(pruned_dom: str, challenge_type: str) -> str | None:
    if challenge_type in ("recaptcha_v2", "hcaptcha", "turnstile"):
        match = _DATA_SITEKEY_RE.search(pruned_dom)
    elif challenge_type == "recaptcha_v3":
        match = _RECAPTCHA_RENDER_RE.search(pruned_dom) or _DATA_SITEKEY_RE.search(pruned_dom)
    elif challenge_type == "funcaptcha":
        match = _FUNCAPTCHA_PUBKEY_RE.search(pruned_dom)
    elif challenge_type == "geetest":
        match = _GEETEST_GT_RE.search(pruned_dom)
    else:
        match = None
    return match.group(1) if match else None


def _metric_inc(metrics: Metrics, name: str, *labels: str) -> None:
    """Increment a named counter when the metrics backend provides one.

    Metric definitions live in the telemetry module; counters it does not
    (yet) define degrade to a no-op rather than failing a navigation.
    """
    counter = getattr(metrics, name, None)
    if counter is None:
        return
    with contextlib.suppress(Exception):
        if labels and hasattr(counter, "labels"):
            counter.labels(*labels).inc()
        elif hasattr(counter, "inc"):
            counter.inc()


class ChallengeDetector:
    """Pure DOM-signature detection; no network."""

    def detect(self, pruned_dom: str, url: str) -> Challenge | None:
        if not pruned_dom:
            return None
        lowered = pruned_dom.lower()
        for challenge_type, markers in _SIGNATURES:
            if any(marker in lowered for marker in markers):
                return Challenge(
                    type=challenge_type,
                    sitekey=_extract_sitekey(pruned_dom, challenge_type),
                    url=url,
                )
        return None


class CapSolverClient:
    def __init__(
        self,
        api_key: str,
        metrics: Metrics,
        logger: logging.Logger,
        poll_interval: float = 2.0,
        timeout: float = 120.0,
        max_concurrent: int = 2,
    ) -> None:
        self._api_key = api_key
        self._metrics = metrics
        self._logger = logger
        self._poll_interval = poll_interval
        self._timeout = timeout
        # CapSolver throttles per key by plan tier; this semaphore keeps the
        # number of concurrently active solves at or below the key's quota.
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def solve(self, challenge: Challenge) -> SolveResult:
        task_type = TASK_TYPES.get(challenge.type)
        if task_type is None:
            raise CapSolverError(f"unsupported challenge type: {challenge.type}")
        task_body = self._build_task(challenge, task_type)
        async with self._semaphore:
            task_id = await self._create_task(task_body)
            deadline = time.monotonic() + self._timeout
            delay = self._poll_interval
            while True:
                await asyncio.sleep(delay)
                if time.monotonic() >= deadline:
                    raise CapSolverError(
                        f"solve for task {task_id} timed out after {self._timeout:.0f}s"
                    )
                status, solution = await self._get_result(task_id)
                if status == "ready":
                    return self._to_solve_result(challenge, task_id, solution)
                delay = min(delay * 2.0, MAX_BACKOFF_SECONDS)

    async def inject(self, page: Any, challenge: Challenge, result: SolveResult) -> None:
        if challenge.type in ("recaptcha_v2", "recaptcha_v3"):
            await page.evaluate(RECAPTCHA_INJECT_JS, result.token)
            await page.evaluate(RECAPTCHA_CALLBACK_JS, result.token)
        elif challenge.type == "hcaptcha":
            await page.evaluate(HCAPTCHA_INJECT_JS, result.token)
            await page.evaluate(GENERIC_CALLBACK_JS, result.token)
        elif challenge.type == "funcaptcha":
            await page.evaluate(FUNCAPTCHA_INJECT_JS, result.token)
            await page.evaluate(GENERIC_CALLBACK_JS, result.token)
        elif challenge.type == "turnstile":
            await page.evaluate(TURNSTILE_INJECT_JS, result.token)
            await page.evaluate(GENERIC_CALLBACK_JS, result.token)
        elif challenge.type == "geetest":
            payload: dict[str, Any] = {}
            if result.token:
                try:
                    payload = json.loads(result.token)
                except ValueError:
                    payload = {}
            await page.evaluate(GEETEST_INJECT_JS, payload)
        else:
            await self._inject_cookie(page, challenge, result)

    async def _inject_cookie(self, page: Any, challenge: Challenge, result: SolveResult) -> None:
        if not (result.cookie_name and result.cookie_value):
            raise CapSolverError(f"solve for {challenge.type} produced no cookie to inject")
        host = urlparse(challenge.url).hostname
        if not host:
            raise CapSolverError(f"cannot derive a cookie domain from page url {challenge.url!r}")
        context = getattr(page, "context", None)
        if context is None or not hasattr(context, "add_cookies"):
            raise CapSolverError(
                f"page has no browser context to set the {challenge.type} cookie into"
            )
        await context.add_cookies(
            [
                {
                    "name": result.cookie_name,
                    "value": result.cookie_value,
                    "domain": host,
                    "path": "/",
                }
            ]
        )

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with exponential backoff; retries 429 / 5xx / errorId 1 up to
        MAX_RATE_LIMIT_RETRIES times, then raises."""
        last_error = "no response"
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            if attempt:
                delay = min(self._poll_interval * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
                self._logger.warning(
                    "capsolver rate-limited (attempt %d); backing off %.2fs",
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"http error: {exc}"
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                continue
            try:
                data = response.json()
            except ValueError:
                raise CapSolverError(
                    f"capsolver returned invalid JSON (HTTP {response.status_code})"
                ) from None
            if data.get("errorId") == 1:
                last_error = f"rate limit: {data.get('errorCode', 'ERROR_RATE_LIMIT')}"
                continue
            return cast(dict[str, Any], data)
        raise CapSolverError(
            f"capsolver rate-limited: gave up after {MAX_RATE_LIMIT_RETRIES} retries ({last_error})"
        )

    async def _create_task(self, task_body: dict[str, Any]) -> str:
        data = await self._post(CREATE_TASK_URL, {"clientKey": self._api_key, "task": task_body})
        if data.get("errorId", 0) != 0:
            raise CapSolverError(
                f"createTask failed: {data.get('errorCode')}: {data.get('errorDescription')}"
            )
        task_id = data.get("taskId")
        if not task_id:
            raise CapSolverError(f"createTask returned no taskId: {data!r}")
        return str(task_id)

    async def _get_result(self, task_id: str) -> tuple[str, dict[str, Any]]:
        data = await self._post(
            GET_TASK_RESULT_URL, {"clientKey": self._api_key, "taskId": task_id}
        )
        if data.get("errorId", 0) != 0:
            raise CapSolverError(
                f"getTaskResult failed: {data.get('errorCode')}: {data.get('errorDescription')}"
            )
        status = data.get("status")
        if status not in ("processing", "ready"):
            raise CapSolverError(f"getTaskResult returned unexpected status: {status!r}")
        solution = data.get("solution") or {}
        return status, solution

    def _build_task(self, challenge: Challenge, task_type: str) -> dict[str, Any]:
        if challenge.type == "datadome":
            raise CapSolverError(
                "DataDome challenge requires proxy parameters (proxyType/"
                "proxyAddress) which circus-tent does not configure; datadome "
                "solves are unsupported until a proxy is configured"
            )
        if challenge.type == "geetest":
            if not challenge.sitekey:
                raise CapSolverError(
                    "GeeTest challenge requires gt/challenge parameters that "
                    "were not present in the page data"
                )
            return {
                "type": task_type,
                "websiteURL": challenge.url,
                "gt": challenge.sitekey,
            }
        task: dict[str, Any] = {"type": task_type, "websiteURL": challenge.url}
        if challenge.type in TOKEN_TYPES:
            if not challenge.sitekey:
                raise CapSolverError(
                    f"{challenge.type} challenge requires a sitekey, but none "
                    "was detected on the page"
                )
            task["websiteKey"] = challenge.sitekey
            if challenge.type == "recaptcha_v3":
                task["minScore"] = RECAPTCHA_V3_REQUESTED_MIN_SCORE
        return task

    def _to_solve_result(
        self, challenge: Challenge, task_id: str, solution: dict[str, Any]
    ) -> SolveResult:
        if challenge.type == "geetest":
            # GeeTest needs several params (validate/seccode/challenge); the
            # frozen SolveResult carries them as a JSON payload in `token`.
            params = {key: value for key, value in solution.items() if isinstance(value, str)}
            return SolveResult(
                task_id=task_id,
                token=json.dumps(params),
                cookie_name=None,
                cookie_value=None,
            )
        if challenge.type in TOKEN_TYPES:
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            if not token:
                raise CapSolverError(
                    f"capsolver solution for {challenge.type} contained no "
                    f"token: {sorted(solution)!r}"
                )
            if challenge.type == "recaptcha_v3":
                with contextlib.suppress(TypeError, ValueError):
                    score = float(solution.get("score", 1.0))
                    if score < RECAPTCHA_V3_ACCEPTABLE_MIN_SCORE:
                        raise CapSolverError(
                            f"reCAPTCHA v3 score {score} below acceptable "
                            f"minimum {RECAPTCHA_V3_ACCEPTABLE_MIN_SCORE}"
                        )
            return SolveResult(
                task_id=task_id, token=str(token), cookie_name=None, cookie_value=None
            )
        cookie_name = COOKIE_NAMES[challenge.type]
        value = solution.get(cookie_name) or solution.get("cookie")
        if not value:
            raise CapSolverError(
                f"capsolver solution for {challenge.type} contained no cookie "
                f"({cookie_name}): {sorted(solution)!r}"
            )
        return SolveResult(
            task_id=task_id,
            token=None,
            cookie_name=cookie_name,
            cookie_value=str(value),
        )


#: In-page JS: write the solved token into the reCAPTCHA response field.
RECAPTCHA_INJECT_JS = """\
(token) => {
  const el = document.getElementById('g-recaptcha-response')
    || document.querySelector('.g-recaptcha-response')
    || document.querySelector('textarea[name="g-recaptcha-response"]');
  if (!el) return false;
  el.value = token;
  return true;
}
"""

#: In-page JS: dispatch the site's own reCAPTCHA callback if registered.
RECAPTCHA_CALLBACK_JS = """\
(token) => {
  try {
    const cfg = window.___grecaptcha_cfg;
    if (!cfg || !cfg.clients) return false;
    for (const id of Object.keys(cfg.clients)) {
      const client = cfg.clients[id];
      if (!client) continue;
      for (const key of Object.keys(client)) {
        const entry = client[key];
        if (entry && typeof entry.callback === 'function') {
          entry.callback(token);
          return true;
        }
      }
    }
  } catch (e) {}
  return false;
}
"""

HCAPTCHA_INJECT_JS = """\
(token) => {
  const el = document.getElementById('h-captcha-response')
    || document.querySelector('.h-captcha-response')
    || document.querySelector('textarea[name="h-captcha-response"]')
    || document.querySelector('input[name="h-captcha-response"]');
  if (!el) return false;
  el.value = token;
  return true;
}
"""

FUNCAPTCHA_INJECT_JS = """\
(token) => {
  const el = document.getElementById('fc-token')
    || document.querySelector('.fc-token')
    || document.querySelector('input[name="fc-token"]');
  if (!el) return false;
  el.value = token;
  return true;
}
"""

TURNSTILE_INJECT_JS = """\
(token) => {
  const el = document.getElementById('cf-turnstile-response')
    || document.querySelector('input[name="cf-turnstile-response"]')
    || document.querySelector('textarea[name="cf-turnstile-response"]');
  if (!el) return false;
  el.value = token;
  return true;
}
"""

#: In-page JS: dispatch a widget callback named by data-callback, if present.
GENERIC_CALLBACK_JS = """\
(token) => {
  try {
    const els = document.querySelectorAll('[data-callback]');
    for (const el of els) {
      const name = el.getAttribute('data-callback');
      if (name && typeof window[name] === 'function') {
        window[name](token);
        return true;
      }
    }
  } catch (e) {}
  return false;
}
"""

GEETEST_INJECT_JS = """\
(payload) => {
  try {
    if (payload.geetest_validate) window.geetest_validate = payload.geetest_validate;
    if (payload.geetest_seccode) window.geetest_seccode = payload.geetest_seccode;
    if (payload.geetest_challenge) window.geetest_challenge = payload.geetest_challenge;
    return true;
  } catch (e) { return false; }
}
"""


class ChallengeResolver:
    def __init__(
        self,
        client: CapSolverClient,
        metrics: Metrics,
        logger: logging.Logger,
        max_solves_per_shard_day: int = 100,
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._logger = logger
        self._max_solves_per_shard_day = max_solves_per_shard_day
        self._detector = ChallengeDetector()
        # f"{shard}:{utc_day}" → charged solves (UTC-day keys, same pattern
        # as the llm budgets module).
        self._daily_solves: dict[str, int] = {}
        # Session solve cache keyed by (shard, type, sitekey): a solved token
        # is re-injected, never re-solved (cost discipline).
        self._cache: dict[tuple[str, str, str | None], SolveResult] = {}
        self._cache_hits = 0
        self._total_solves = 0

    async def handle(self, page: Any, pruned_dom: str, url: str, shard: str) -> bool:
        try:
            challenge = self._detector.detect(pruned_dom, url)
            if challenge is None:
                return False
            day_key = _utc_day_key()
            self._daily_solves = {
                key: count
                for key, count in self._daily_solves.items()
                if key.endswith(f":{day_key}")
            }
            budget_key = f"{shard}:{day_key}"
            if self._daily_solves.get(budget_key, 0) >= self._max_solves_per_shard_day:
                self._logger.warning(
                    "solve budget exceeded for shard %s on %s (cap %d); skipping solve",
                    shard,
                    day_key,
                    self._max_solves_per_shard_day,
                )
                _metric_inc(self._metrics, "challenge_budget_exceeded", shard)
                return False
            cache_key = (shard, challenge.type, challenge.sitekey)
            cached = self._cache.get(cache_key)
            if cached is not None:
                result = cached
                self._cache_hits += 1
                _metric_inc(self._metrics, "challenge_cache_hits", shard)
                self._logger.info("reusing cached solve for %s on shard %s", challenge.type, shard)
            else:
                result = await self._client.solve(challenge)
                self._cache[cache_key] = result
                self._daily_solves[budget_key] = self._daily_solves.get(budget_key, 0) + 1
                self._total_solves += 1
                _metric_inc(self._metrics, "challenge_solves", shard)
            await self._client.inject(page, challenge, result)
            return True
        except Exception as exc:
            # Never raise into the engine: the caller proceeds as a normal
            # navigation failure. Traceback is kept at debug level.
            self._logger.error("challenge resolution failed on shard %s: %s", shard, exc)
            self._logger.debug("challenge resolution traceback", exc_info=True)
            return False

    def snapshot(self) -> dict[str, Any]:
        today = _utc_day_key()
        return {
            "daily_solves": {
                key.split(":", 1)[0]: count
                for key, count in self._daily_solves.items()
                if key.endswith(f":{today}")
            },
            "cache_hits": self._cache_hits,
            "cache_entries": len(self._cache),
            "total_solves": self._total_solves,
        }
