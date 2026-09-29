"""LLM router: Gemini → OpenRouter failover, backoff, metering fields (01 §7)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from api.config import Settings, get_settings
from api.llm.router import LLMRequestError, LLMResult, LLMRouter, LLMUnavailableError


def ok(content: str = "hi", *, tool_calls: list[dict[str, object]] | None = None) -> httpx.Response:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
        },
    )


class Graph:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def router(graph: Graph, sleeps: list[float], **overrides: object) -> LLMRouter:
    base: Settings = get_settings()
    settings = base.model_copy(
        update={
            "google_ai_api_key": SecretStr("g-key"),
            "openrouter_api_key": SecretStr("or-key"),
            **overrides,
        }
    )

    async def sleep(s: float) -> None:
        sleeps.append(s)

    return LLMRouter(httpx.AsyncClient(transport=httpx.MockTransport(graph)), settings, sleep=sleep)


async def chat(r: LLMRouter, **kw: Any) -> LLMResult:
    return await r.chat(
        provider="gemini",
        model="gemini-2.5-flash-lite",
        messages=[{"role": "user", "content": "x"}],
        **kw,
    )


async def test_primary_success_records_tokens_latency_cost() -> None:
    g = Graph(ok("hello"))
    res = await chat(router(g, []))
    assert res.content == "hello"
    assert (res.provider, res.model) == ("gemini", "gemini-2.5-flash-lite")
    assert (res.prompt_tokens, res.completion_tokens) == (1000, 200)
    # 1000 * 0.10/M + 200 * 0.40/M
    assert res.cost_usd == Decimal("0.00018")
    req = g.requests[0]
    assert str(req.url).startswith(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert req.headers["authorization"] == "Bearer g-key"


async def test_two_retries_with_spec_backoff_then_success() -> None:
    sleeps: list[float] = []
    g = Graph(httpx.Response(503), httpx.Response(429), ok())
    await chat(router(g, sleeps))
    assert sleeps == [0.5, 2.0]
    assert len(g.requests) == 3


async def test_failover_to_openrouter_with_equivalent_model() -> None:
    sleeps: list[float] = []
    g = Graph(
        httpx.Response(500), httpx.Response(500), httpx.ReadTimeout("slow"), ok("from openrouter")
    )
    res = await chat(router(g, sleeps))
    assert res.provider == "openrouter"
    assert json.loads(g.requests[-1].content)["model"] == "google/gemini-2.5-flash-lite"
    assert g.requests[-1].headers["authorization"] == "Bearer or-key"


async def test_both_down_raises_unavailable() -> None:
    g = Graph(*[httpx.Response(503)] * 4)
    with pytest.raises(LLMUnavailableError):
        await chat(router(g, []))
    assert len(g.requests) == 4  # 3 on primary, 1 on failover


async def test_bad_request_is_not_retried_or_failed_over() -> None:
    g = Graph(httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(LLMRequestError):
        await chat(router(g, []))
    assert len(g.requests) == 1


async def test_provider_without_key_is_skipped() -> None:
    g = Graph(ok("via openrouter"))
    res = await chat(router(g, [], google_ai_api_key=None))
    assert res.provider == "openrouter"


async def test_no_keys_at_all_is_unavailable() -> None:
    with pytest.raises(LLMUnavailableError):
        await chat(router(Graph(), [], google_ai_api_key=None, openrouter_api_key=None))


async def test_tool_calls_and_json_mode_request_shape() -> None:
    g = Graph(
        ok(
            "",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_products", "arguments": '{"category":"water"}'},
                }
            ],
        )
    )
    res = await chat(
        router(g, []),
        tools=[{"type": "function", "function": {"name": "get_products"}}],
        json_mode=True,
    )
    assert [(c.name, c.arguments) for c in res.tool_calls] == [
        ("get_products", '{"category":"water"}')
    ]
    body = json.loads(g.requests[0].content)
    assert body["tool_choice"] == "auto"
    assert body["response_format"] == {"type": "json_object"}


async def test_unknown_model_price_is_zero_not_guessed() -> None:
    g = Graph(ok())
    r = router(g, [])
    res = await r.chat(provider="gemini", model="some-new-model", messages=[])
    assert res.cost_usd == Decimal(0)
