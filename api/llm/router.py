"""LLM router — Gemini primary → OpenRouter failover (01_architecture §7).

Both providers speak the OpenAI Chat Completions shape, so one thin httpx client serves both.
No LiteLLM, no SDK.

* Per-tenant provider/model (tenant_settings) — moving a client to a stronger model is an UPDATE.
* Primary: up to 2 retries with 0.5 s / 2 s backoff on 429 / 5xx / timeout / connect errors.
* Then one attempt on the other provider with the equivalent model id.
* Both down → LLMUnavailableError; the caller sends a holding message and re-queues the turn.
* A 404 means this provider does not serve the model (retired, or never existed under that id):
  not retried, but failed over — the other provider may still have it, and a reply beats silence.
  If every provider 404s it is a config bug: LLMRequestError.
* Any other non-429 4xx is our bug (bad request), not an outage: LLMRequestError, no failover.
* Thinking (Gemini 3.x thinks by default and it eats max_tokens) is turned down per model, on
  both providers: see _reasoning_effort.
* Thought signatures come back on tool calls (Gemini: tool_calls[].extra_content; OpenRouter:
  message.reasoning_details) and must be sent back with the tool-call history, or Gemini 3
  rejects the next round with a 400. assistant_message() carries them; _for_provider strips the
  other provider's field after a failover.
* Every call returns model, prompt/completion tokens, latency and USD cost for metering.
* Request and response bodies (customer text) are never logged. A rejected request logs the
  provider's error message (truncated), which describes our request, not the conversation.
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
_ERROR_DETAIL_MAX: Final = 300

Sleep = Callable[[float], Awaitable[None]]


class LLMUnavailableError(Exception):
    """Every provider failed with a retryable error."""


class LLMRequestError(Exception):
    """The provider rejected the request (4xx other than 429). Not retried, no failover."""


class _RetryableError(Exception):
    pass


class _ModelNotFoundError(Exception):
    """HTTP 404 from one provider: skip its retries, try the other provider."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, validated by the tool's Pydantic model
    extra_content: dict[str, Any] | None = None  # Gemini's thought signature rides here


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
    reasoning_details: list[Any] | None = None  # OpenRouter's thought signatures

    def assistant_message(self) -> dict[str, Any]:
        """The assistant turn for the history, signatures included (sent back unmodified)."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                    **({"extra_content": c.extra_content} if c.extra_content else {}),
                }
                for c in self.tool_calls
            ]
        if self.reasoning_details:
            msg["reasoning_details"] = self.reasoning_details
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
        outage = False
        for index, (prov, prov_model) in enumerate(plan):
            retries = RETRY_BACKOFF_S if index == 0 else ()
            try:
                return await self._call_with_retries(prov, prov_model, body, retries)
            except (_RetryableError, _ModelNotFoundError) as exc:
                if isinstance(exc, _ModelNotFoundError):
                    # Loud: the tenant's model id needs changing, failover only hides it.
                    log.error("llm_model_not_found", provider=prov.name, model=prov_model)
                else:
                    outage = True
                    log.warning(
                        "llm_provider_failed", provider=prov.name, model=prov_model, reason=str(exc)
                    )
                if index + 1 < len(plan):
                    log.error(
                        "llm_failover", from_provider=prov.name, to_provider=plan[index + 1][0].name
                    )
                    await self._event("failover")
        if not outage:
            raise LLMRequestError(f"no provider serves model {model!r}: HTTP 404")
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
        payload = {**body, "model": model, "messages": _for_provider(body["messages"], prov.name)}
        effort = self._reasoning_effort(model)
        if effort and prov.name == "gemini":
            payload["reasoning_effort"] = effort
        elif effort:
            payload["reasoning"] = {"effort": effort}  # OpenRouter's unified parameter
        started = time.perf_counter()
        try:
            resp = await self._http.post(url, json=payload, headers=headers, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise _RetryableError(f"timeout:{type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise _RetryableError(f"transport:{type(exc).__name__}") from None
        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code in RETRY_STATUSES:
            raise _RetryableError(f"http_{resp.status_code}")
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            log.error(
                "llm_request_rejected",
                provider=prov.name,
                model=model,
                status=resp.status_code,
                detail=detail,
            )
            if resp.status_code == 404:
                raise _ModelNotFoundError(f"{prov.name} has no model {model!r}: HTTP 404")
            raise LLMRequestError(
                f"{prov.name} rejected the request: HTTP {resp.status_code}: {detail}"
            )
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
                extra_content=tc.get("extra_content")
                if isinstance(tc.get("extra_content"), dict)
                else None,
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
            reasoning_details=message.get("reasoning_details")
            if isinstance(message.get("reasoning_details"), list)
            else None,
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

    def _reasoning_effort(self, model: str) -> str | None:
        """GEMINI_REASONING_EFFORT for a Gemini model id, on whichever provider serves it.

        "none" turns thinking off on Gemini 2.x only. Gemini 3.x cannot stop thinking: Google's
        OpenAI-compatibility page says so, OpenRouter lists those models as reasoning-mandatory,
        and gemini-3.5-flash-lite answered "none" with HTTP 400 on prod. "minimal" is the lowest
        level every 3.x model accepts, so "none" becomes "minimal" there.
        """
        effort = self._settings.gemini_reasoning_effort
        bare = model.removeprefix(self._settings.openrouter_model_prefix)
        if not effort or not bare.startswith("gemini-"):
            return None  # not a Gemini model (e.g. another vendor on OpenRouter): its default
        if effort == "none" and not bare.startswith("gemini-2"):
            return "minimal"
        return effort

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
        key = model.removeprefix(self._settings.openrouter_model_prefix)
        prices = self._settings.llm_prices_usd_per_mtok.get(key)
        if prices is None:
            log.warning("llm_price_unknown", model=key)
            return Decimal(0)
        return (prices[0] * prompt_tokens + prices[1] * completion_tokens) / _MTOK


def _for_provider(messages: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Drop the other provider's signature field (history built before a failover)."""
    if provider == "gemini":
        if not any("reasoning_details" in m for m in messages):
            return messages
        return [{k: v for k, v in m.items() if k != "reasoning_details"} for m in messages]
    if not any("extra_content" in tc for m in messages for tc in m.get("tool_calls") or ()):
        return messages
    return [
        {
            **m,
            "tool_calls": [
                {k: v for k, v in tc.items() if k != "extra_content"} for tc in m["tool_calls"]
            ],
        }
        if m.get("tool_calls")
        else m
        for m in messages
    ]


def _error_detail(resp: httpx.Response) -> str:
    """The provider's error message (Gemini sends a dict or a one-element list), truncated."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:_ERROR_DETAIL_MAX]
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:_ERROR_DETAIL_MAX]
    return str(err or data)[:_ERROR_DETAIL_MAX]


def _as_json_string(arguments: object) -> str:
    if isinstance(arguments, str):
        return arguments or "{}"
    return json.dumps(arguments or {})
