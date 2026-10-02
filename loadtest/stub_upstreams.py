"""Stand-ins for the two outside services during a load test (never used anywhere else).

    uvicorn loadtest.stub_upstreams:app --port 9100

* /llm/chat/completions — OpenAI-compatible, like Gemini's endpoint. Latency drawn from what
  the real models take (classifier ~350 ms, support reply ~1.1 s, both with a long tail), so the
  test measures OUR overhead on top of a realistic model, not a zero-latency fake.
* /graph/<version>/<phone_number_id>/messages — Meta's send API. Records when each reply to each
  customer number arrived; GET /sent/<wa_id> lets the load generator see its reply land.

Point the platform at it with GEMINI_BASE_URL=http://127.0.0.1:9100/llm/ and
META_GRAPH_BASE_URL=http://127.0.0.1:9100/graph (and a dummy GOOGLE_AI_API_KEY).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
# varied, number-free replies (the validator rejects invented numbers and a reply identical to
# the previous one), each identifying itself as automated (first-reply AI disclosure)
REPLIES = [
    "Hi, I'm Sara, the automated assistant. Yes, we deliver to your area every day.",
    "This is Sara, an automated assistant. You can change your address any time here.",
    "I'm Sara, the automated assistant. The bottles are returnable on your next delivery.",
    "Sara here, the automated assistant. We're open tomorrow from the morning until late evening.",
    "Hello, I'm Sara, the automated assistant. Happy to help with your delivery.",
    "I'm Sara, an automated assistant. Our team can confirm the details if you need.",
    "Sara, the automated assistant, here. Let me know what else you'd like to know.",
]


def _reply(messages: list[dict[str, Any]]) -> str:
    """Any reply but the one this conversation got last time (a repeat escalates by design)."""
    last = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "assistant"), None
    )
    return random.choice([r for r in REPLIES if r != last])  # noqa: S311


SENT: dict[str, list[float]] = defaultdict(list)

CLASSIFY_MS = (float(os.environ.get("STUB_CLASSIFY_MS", "350")), 0.35)  # median, spread
REPLY_MS = (float(os.environ.get("STUB_REPLY_MS", "1100")), 0.35)


def _latency(median_ms: float, sigma: float) -> float:
    return random.lognormvariate(0, sigma) * median_ms / 1000


@app.post("/llm/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body: dict[str, Any] = await request.json()
    classify = body.get("response_format", {}).get("type") == "json_object"
    await asyncio.sleep(_latency(*(CLASSIFY_MS if classify else REPLY_MS)))
    content = (
        json.dumps({"intent": "support", "language": "en", "confidence": 0.93})
        if classify
        else _reply(body.get("messages", []))
    )
    return JSONResponse(
        {
            "id": uuid.uuid4().hex,
            "model": body.get("model", "stub"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 1400 if not classify else 300, "completion_tokens": 30},
        }
    )


@app.post("/graph/{version}/{pnid}/messages")
async def send(version: str, pnid: str, request: Request) -> JSONResponse:
    body = await request.json()
    await asyncio.sleep(_latency(120, 0.3))  # Graph API round trip
    SENT[body["to"]].append(time.time())
    return JSONResponse(
        {"contacts": [{"wa_id": body["to"]}], "messages": [{"id": f"wamid.L{uuid.uuid4().hex}"}]}
    )


@app.get("/sent/{wa_id}")
async def sent(wa_id: str) -> dict[str, list[float]]:
    return {"at": SENT.get(wa_id, [])}


@app.get("/stats")
async def stats() -> dict[str, int]:
    return {"customers": len(SENT), "replies": sum(len(v) for v in SENT.values())}
