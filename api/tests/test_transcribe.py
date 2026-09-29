"""Voice-note transcription via Gemini's native audio input."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from api.config import get_settings
from api.llm.router import LLMUnavailableError
from api.llm.transcribe import GeminiTranscriber


def transcriber(handler: httpx.MockTransport, *, key: str | None = "g-key") -> GeminiTranscriber:
    settings = get_settings().model_copy(
        update={"google_ai_api_key": SecretStr(key) if key else None}
    )
    return GeminiTranscriber(httpx.AsyncClient(transport=handler), settings)


def gemini_ok(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 320, "candidatesTokenCount": 12},
        },
    )


async def test_ogg_goes_inline_and_key_goes_in_a_header_not_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return gemini_ok(" كم زجاجة باقي عندي ")

    tr = await transcriber(httpx.MockTransport(handler)).transcribe(
        b"OggS", "audio/ogg; codecs=opus"
    )
    assert tr.text == "كم زجاجة باقي عندي"
    assert (tr.prompt_tokens, tr.completion_tokens) == (320, 12)
    req = seen[0]
    assert "key=" not in str(req.url)
    assert req.headers["x-goog-api-key"] == "g-key"
    assert req.url.path.endswith(":generateContent")
    part = json.loads(req.content)["contents"][0]["parts"][1]["inline_data"]
    assert part["mime_type"] == "audio/ogg"
    assert base64.b64decode(part["data"]) == b"OggS"


async def test_retries_a_5xx_once() -> None:
    calls = iter([httpx.Response(503), gemini_ok("hello")])
    tr = await transcriber(httpx.MockTransport(lambda _r: next(calls))).transcribe(b"x", None)
    assert tr.text == "hello"


async def test_persistent_failure_raises_unavailable() -> None:
    with pytest.raises(LLMUnavailableError):
        await transcriber(httpx.MockTransport(lambda _r: httpx.Response(500))).transcribe(
            b"x", None
        )
    with pytest.raises(LLMUnavailableError):
        await transcriber(httpx.MockTransport(lambda _r: httpx.Response(400))).transcribe(
            b"x", None
        )
    with pytest.raises(LLMUnavailableError):
        await transcriber(httpx.MockTransport(lambda _r: httpx.Response(200, json={}))).transcribe(
            b"x", None
        )


async def test_no_key_is_unavailable_without_a_request() -> None:
    def never(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(LLMUnavailableError):
        await transcriber(httpx.MockTransport(never), key=None).transcribe(b"x", None)
