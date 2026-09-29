"""Voice-note transcription via Gemini's native API (audio/ogg inline).

WhatsApp voice notes are OGG/Opus. The OpenAI-compatible endpoint's `input_audio` accepts only
wav/mp3, so this uses the native generateContent endpoint, which takes audio/ogg directly.
The key travels in the x-goog-api-key header (never in the URL). Transcripts are stored in
messages.transcript and never logged.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx

from api.config import Settings
from api.core.logging import get_logger
from api.llm.router import LLMUnavailableError

log = get_logger(__name__)

INSTRUCTION = (
    "Transcribe this WhatsApp voice note exactly as spoken, in the language and script the speaker "
    "used. Return only the transcript text, with no commentary. If it is silent or unintelligible, "
    "return an empty string."
)


@dataclass(frozen=True)
class Transcript:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost_usd: Decimal


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, mime_type: str | None) -> Transcript: ...


class GeminiTranscriber:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    async def transcribe(self, audio: bytes, mime_type: str | None) -> Transcript:
        key = self._settings.google_ai_api_key
        if key is None:
            raise LLMUnavailableError("GOOGLE_AI_API_KEY not configured for transcription")
        model = self._settings.transcription_model
        mime = (mime_type or "audio/ogg").split(";")[0].strip()
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": INSTRUCTION},
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(audio).decode(),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        }
        url = f"{self._settings.gemini_native_base_url.rstrip('/')}/models/{model}:generateContent"
        started = time.perf_counter()
        for attempt in range(2):
            try:
                resp = await self._http.post(
                    url,
                    json=body,
                    headers={"x-goog-api-key": key.get_secret_value()},
                    timeout=httpx.Timeout(self._settings.llm_timeout_s * 2, connect=5.0),
                )
            except httpx.TransportError as exc:
                log.warning(
                    "transcribe_transport_error", attempt=attempt + 1, error=type(exc).__name__
                )
                continue
            if resp.status_code == 200:
                break
            log.warning("transcribe_http_error", attempt=attempt + 1, status=resp.status_code)
            if resp.status_code < 500 and resp.status_code != 429:
                break
        else:
            raise LLMUnavailableError("transcription failed")
        if resp.status_code != 200:
            raise LLMUnavailableError(f"transcription failed: HTTP {resp.status_code}")
        try:
            data = resp.json()
            parts = data["candidates"][0]["content"].get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError("malformed transcription response") from exc
        usage = data.get("usageMetadata") or {}
        pt, ct = (
            int(usage.get("promptTokenCount") or 0),
            int(usage.get("candidatesTokenCount") or 0),
        )
        prices = self._settings.llm_prices_usd_per_mtok.get(model)
        cost = (prices[0] * pt + prices[1] * ct) / Decimal(1_000_000) if prices else Decimal(0)
        return Transcript(
            text=text,
            model=model,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=cost,
        )
