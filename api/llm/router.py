"""LLM router — Gemini primary → OpenRouter failover (01_architecture §7).

Both providers speak the OpenAI Chat Completions shape, so one thin httpx client serves both.
No LiteLLM, no SDK.

* Per-tenant provider/model (tenant_settings) — moving a client to a stronger model is an UPDATE.
* Primary: up to 2 retries with 0.5 s / 2 s backoff on 429 / 5xx / timeout / connect errors.
* Then one attempt on the other provider with the equivalent model id.
* Both down → LLMUnavailableError; the caller sends a holding message and re-queues the turn.
* A non-429 4xx is our bug (bad request), not an outage: raised as LLMRequestError, no failover.
* Every call returns model, prompt/completion tokens, latency and USD cost for metering.
* Request and response bodies (customer text) are never logged.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Protocol

import httpx

from api.config import Settings
from api.core.logging import get_logger

log = get_logger(__name__)

RETRY_BACKOFF_S: Final = (0.5, 2.0)
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})
_MTOK: Final = Decimal(1_000_000)

Sleep = Callable[[float], Awaitable[None]]


class LLMUnavailableError(Exception):
    """Every provider failed with a retryable error."""


class LLMRequestError(Exception):
    """The provider rejected the request (4xx other than 429). Not retried, no failover."""


class _RetryableError(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, validated by the tool's Pydantic model


@dataclass(frozen=True)
class LLMResult:
    content: str | None
    tool_calls: list[ToolCall]
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost_usd: Decimal
    finish_reason: str | None = None

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return msg


class LLM(Protocol):
    async def chat(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 300,
        json_mode: bool = False,
    ) -> LLMResult: ...


@dataclass
class _Provider:
    name: str
    base_url: str
    api_key: str | None
    extra_headers: dict[str, str] = field(default_factory=dict)


class LLMRouter:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        *,
        sleep: Sleep = asyncio.sleep,
        on_event: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        # on_event("failover" | "all_down"): the worker counts these for the alert watchdog
        self._on_event = on_event
        self._http = http
        self._settings = settings
        self._sleep = sleep
        self._timeout = httpx.Timeout(
            settings.llm_timeout_s, connect=settings.llm_connect_timeout_s
        )
        self._providers = {
            "gemini": _Provider(
                "gemini",
                settings.gemini_base_url,
                settings.google_ai_api_key.get_secret_value()
                if settings.google_ai_api_key
                else None,
            ),
            "openrouter": _Provider(
                "openrouter",
                settings.openrouter_base_url,
                settings.openrouter_api_key.get_secret_value()
                if settings.openrouter_api_key
                else None,
                {"X-Title": "HMH Labz Agent Platform"},
            ),
        }

    # ------------------------------------------------------------ public

    async def chat(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 300,
        json_mode: bool = False,
    ) -> LLMResult:
        body: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        plan = self._plan(provider, model)
        if not plan:
            raise LLMUnavailableError("no LLM provider has an API key configured")
        for index, (prov, prov_model) in enumerate(plan):
            retries = RETRY_BACKOFF_S if index == 0 else ()
            try:
                return await self._call_with_retries(prov, prov_model, body, retries)
            except _RetryableError as exc:
                log.warning(
                    "llm_provider_failed", provider=prov.name, model=prov_model, reason=str(exc)
                )
                if index + 1 < len(plan):
                    log.error(
                        "llm_failover", from_provider=prov.name, to_provider=plan[index + 1][0].name
                    )
                    await self._event("failover")
        log.error("llm_all_providers_down", providers=[p.name for p, _ in plan])
        await self._event("all_down")
        raise LLMUnavailableError("all LLM providers failed")

    # ------------------------------------------------------------ internals

    async def _event(self, name: str) -> None:
        if self._on_event is not None:
            await self._on_event(name)

    def _plan(self, provider: str, model: str) -> list[tuple[_Provider, str]]:
        prefix = self._settings.openrouter_model_prefix
        if provider == "openrouter":
            primary = (self._providers["openrouter"], model if "/" in model else prefix + model)
            fb_model = model.removeprefix(prefix) if model.startswith(prefix) else None
            fallback = (self._providers["gemini"], fb_model) if fb_model else None
        else:
            primary = (self._providers["gemini"], model.removeprefix(prefix))
            fallback = (self._providers["openrouter"], model if "/" in model else prefix + model)
        plan = [primary] + ([fallback] if fallback else [])
        return [(p, m) for p, m in plan if p.api_key]

    async def _call_with_retries(
        self, prov: _Provider, model: str, body: dict[str, Any], backoff: tuple[float, ...]
    ) -> LLMResult:
        attempt = 0
        while True:
            try:
                return await self._call(prov, model, body)
            except _RetryableError:
                if attempt >= len(backoff):
                    raise
                await self._sleep(backoff[attempt])
                attempt += 1

    async def _call(self, prov: _Provider, model: str, body: dict[str, Any]) -> LLMResult:
        url = prov.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {prov.api_key}", **prov.extra_headers}
        started = time.perf_counter()
        try:
            resp = await self._http.post(
                url, json={**body, "model": model}, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise _RetryableError(f"timeout:{type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise _RetryableError(f"transport:{type(exc).__name__}") from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code in RETRY_STATUSES:
            raise _RetryableError(f"http_{resp.status_code}")
        if resp.status_code >= 400:
            raise LLMRequestError(f"{prov.name} rejected the request: HTTP {resp.status_code}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _RetryableError("malformed_response") from exc

        tool_calls = [
            ToolCall(
                id=str(tc.get("id") or f"call_{i}"),
                name=str(tc["function"]["name"]),
                arguments=_as_json_string(tc["function"].get("arguments")),
            )
            for i, tc in enumerate(message.get("tool_calls") or [])
            if isinstance(tc, dict) and tc.get("function")
        ]
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        result = LLMResult(
            content=message.get("content"),
            tool_calls=tool_calls,
            provider=prov.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=self.cost(model, prompt_tokens, completion_tokens),
            finish_reason=choice.get("finish_reason"),
        )
        log.info(
            "llm_call",
            provider=prov.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            tool_calls=len(tool_calls),
        )
        return result

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
        key = model.removeprefix(self._settings.openrouter_model_prefix)
        prices = self._settings.llm_prices_usd_per_mtok.get(key)
        if prices is None:
            log.warning("llm_price_unknown", model=key)
            return Decimal(0)
        return (prices[0] * prompt_tokens + prices[1] * completion_tokens) / _MTOK


def _as_json_string(arguments: object) -> str:
    if isinstance(arguments, str):
        return arguments or "{}"
    return json.dumps(arguments or {})
