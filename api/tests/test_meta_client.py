"""Graph API client: timeouts, retries, Retry-After, no duplicate sends, no token leaks."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from api.meta.client import MetaAPIError, MetaClient, MetaMediaTooLargeError

TOKEN = "EAAG-test-token-never-logged"
PNID = "123456789012345"

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def client(handler: Handler, sleeps: list[float] | None = None, **kw: object) -> MetaClient:
    async def fake_sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    return MetaClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        access_token=TOKEN,
        phone_number_id=PNID,
        api_version="v21.0",
        sleep=fake_sleep,
        **kw,  # type: ignore[arg-type]
    )


def sent(wamid: str = "wamid.OUT1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": "971501234567", "wa_id": "971501234567"}],
            "messages": [{"id": wamid, "message_status": "accepted"}],
        },
    )


async def test_send_text_request_shape_and_auth() -> None:
    rec = Recorder(sent())
    result = await client(rec).send_text("971501234567", "Hello")
    assert result.wamid == "wamid.OUT1"
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == f"/v21.0/{PNID}/messages"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(req.content)
    assert body["to"] == "971501234567"
    assert body["type"] == "text"
    assert body["text"]["body"] == "Hello"


async def test_send_template_and_interactive_shapes() -> None:
    rec = Recorder(sent(), sent())
    c = client(rec)
    await c.send_template(
        "971501234567", "order_update", "en", [{"type": "body", "parameters": []}]
    )
    await c.send_interactive("971501234567", {"type": "button", "body": {"text": "?"}})
    t, i = (json.loads(r.content) for r in rec.requests)
    assert t["template"] == {
        "name": "order_update",
        "language": {"code": "en"},
        "components": [{"type": "body", "parameters": []}],
    }
    assert i["type"] == "interactive"


async def test_429_honours_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    rec = Recorder(httpx.Response(429, headers={"retry-after": "7"}, json={"error": {}}), sent())
    result = await client(rec, sleeps).send_text("971501234567", "hi")
    assert result.wamid == "wamid.OUT1"
    assert len(rec.requests) == 2
    assert sleeps[0] >= 7


async def test_retry_after_is_capped() -> None:
    sleeps: list[float] = []
    rec = Recorder(httpx.Response(503, headers={"retry-after": "3600"}), sent())
    await client(rec, sleeps).send_text("971501234567", "hi")
    assert sleeps[0] <= 30


async def test_5xx_exhausts_retries_then_raises() -> None:
    rec = Recorder(*[httpx.Response(500, json={"error": {"message": "boom", "code": 1}})] * 4)
    with pytest.raises(MetaAPIError) as exc:
        await client(rec, max_retries=3).send_text("971501234567", "hi")
    assert len(rec.requests) == 4
    assert exc.value.status_code == 500


async def test_4xx_is_not_retried_and_error_is_parsed() -> None:
    rec = Recorder(
        httpx.Response(
            400,
            json={
                "error": {
                    "message": "(#131030) Recipient not in allowed list",
                    "code": 131030,
                    "fbtrace_id": "abc",
                }
            },
        )
    )
    with pytest.raises(MetaAPIError) as exc:
        await client(rec).send_text("971501234567", "hi")
    assert len(rec.requests) == 1
    assert (exc.value.code, exc.value.fbtrace_id) == (131030, "abc")


async def test_read_timeout_on_send_is_not_retried() -> None:
    """The send may have reached Meta; a retry could message the customer twice."""
    rec = Recorder(httpx.ReadTimeout("slow"), sent())
    with pytest.raises(MetaAPIError):
        await client(rec).send_text("971501234567", "hi")
    assert len(rec.requests) == 1


async def test_connect_error_on_send_is_retried() -> None:
    rec = Recorder(httpx.ConnectError("refused"), sent())
    assert (await client(rec).send_text("971501234567", "hi")).wamid == "wamid.OUT1"
    assert len(rec.requests) == 2


async def test_read_timeout_on_get_is_retried() -> None:
    rec = Recorder(
        httpx.ReadTimeout("slow"),
        httpx.Response(
            200,
            json={"data": [{"id": "1", "name": "promo", "status": "APPROVED", "language": "en"}]},
        ),
    )
    templates = await client(rec).get_template_status("555", "promo")
    assert [(t.name, t.status) for t in templates] == [("promo", "APPROVED")]
    assert len(rec.requests) == 2


async def test_download_media_two_step_with_auth() -> None:
    rec = Recorder(
        httpx.Response(
            200,
            json={
                "url": "https://lookaside.fbsbx.com/media/abc",
                "mime_type": "audio/ogg",
                "file_size": 3,
            },
        ),
        httpx.Response(200, content=b"OGG"),
    )
    media = await client(rec).download_media("MEDIA1")
    assert (media.content, media.mime_type) == (b"OGG", "audio/ogg")
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in rec.requests)


async def test_download_media_size_cap() -> None:
    rec = Recorder(httpx.Response(200, json={"url": "https://cdn.example/x", "file_size": 10_000}))
    with pytest.raises(MetaMediaTooLargeError):
        await client(rec, media_max_bytes=1000).download_media("M")


async def test_every_request_has_a_timeout() -> None:
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return sent()

    await client(handler).send_text("971501234567", "hi")
    assert seen[0]["read"] is not None
    assert seen[0]["connect"] is not None


async def test_token_never_in_errors_or_repr() -> None:
    rec = Recorder(
        httpx.ConnectError(f"refused {TOKEN}"),
        httpx.ConnectError("x"),
        httpx.ConnectError("x"),
        httpx.ConnectError("x"),
    )
    c = client(rec)
    with pytest.raises(MetaAPIError) as exc:
        await c.send_text("971501234567", "hi")
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None
    assert TOKEN not in repr(c)
