"""Telegram as the second channel (06_multichannel_telegram.md §10).

The Bot API is faked at the HTTP layer (FakeTelegram); Postgres and Redis are real. Covers the
webhook's order (key → secret → parse), ingest and identity, the formatter and splitter, sending
and blocking, the turn runner on a bot, consent and STOP, and Telegram campaigns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from api.agents.turn import TurnRunner
from api.channels import inbound
from api.channels.errors import RecipientUnreachableError
from api.channels.outbound import send_text_reply
from api.channels.telegram.client import TelegramAPIError, TelegramClient
from api.channels.telegram.format import split_text, to_telegram_html
from api.channels.telegram.ingest import TelegramIngestor
from api.channels.telegram.payloads import Message as TgMessage
from api.channels.telegram.payloads import Update, normalise, start_payload
from api.channels.telegram.router import TelegramRouter
from api.channels.telegram.sender import TelegramSender
from api.config import Settings
from api.core.crypto import encrypt_secret
from api.db.models import (
    Campaign,
    CampaignRecipient,
    Conversation,
    Customer,
    CustomerIdentity,
    Message,
    OptinLink,
    Tenant,
    TenantChannel,
    UsageDaily,
    WebhookEvent,
)
from api.db.session import Database
from api.main import create_app
from api.metering import usage_day
from api.modules.campaigns import service
from api.modules.campaigns.sender import run_campaign
from api.modules.registry import enabled_of
from api.optin import service as optin
from api.tests.conftest import RecordingEnqueuer, make_channel, make_tenant
from api.tests.test_campaigns import BINDINGS, CLOCK, WATER, FakeRedis, scope
from api.tests.test_conversations import (  # noqa: F401 — `shop` is a fixture used below
    FakeTranscriber,
    ScriptedLLM,
    Shop,
    say,
    shop,
)

# ================================================================ fake Bot API


@dataclass
class FakeTelegram:
    """api.telegram.org at the HTTP layer, for any number of bots (keyed by the token's id)."""

    blocked: set[str] = field(default_factory=set)  # chat ids that blocked the bot
    reject_html: bool = False  # answer "can't parse entities" to HTML messages
    reject_token: bool = False
    sent: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    webhooks: dict[str, dict[str, Any]] = field(default_factory=dict)  # bot id → setWebhook body
    deleted: list[str] = field(default_factory=list)
    file = b"OggS-fake-voice-note"
    next_id: int = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        m = re.match(r"^/(file/)?bot(\d+):[^/]+/(.+)$", request.url.path)
        if m is None:
            return httpx.Response(404, json={"ok": False, "description": "Not Found"})
        is_file, bot, method = bool(m.group(1)), m.group(2), m.group(3)
        if is_file:
            return httpx.Response(200, content=self.file)
        if self.reject_token:
            return httpx.Response(401, json={"ok": False, "error_code": 401,
                                             "description": "Unauthorized"})  # fmt: skip
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return _ok({"id": int(bot), "is_bot": True, "first_name": "Bot", "username": f"b{bot}"})
        if method == "setWebhook":
            self.webhooks[bot] = body
            return _ok(True)
        if method == "getWebhookInfo":
            return _ok({"url": self.webhooks.get(bot, {}).get("url", ""),
                        "pending_update_count": 0})  # fmt: skip
        if method == "deleteWebhook":
            self.deleted.append(bot)
            self.webhooks.pop(bot, None)
            return _ok(True)
        if method == "sendChatAction":
            self.actions.append(body)
            return _ok(True)
        if method == "getFile":
            return _ok({"file_id": body["file_id"], "file_size": len(self.file),
                        "file_path": "voice/file_1.oga"})  # fmt: skip
        if method == "sendMessage":
            chat = str(body["chat_id"])
            if chat in self.blocked:
                return httpx.Response(403, json={"ok": False, "error_code": 403,
                    "description": "Forbidden: bot was blocked by the user"})  # fmt: skip
            if self.reject_html and body.get("parse_mode") == "HTML":
                return httpx.Response(400, json={"ok": False, "error_code": 400,
                    "description": "Bad Request: can't parse entities"})  # fmt: skip
            self.sent.append(body)
            self.next_id += 1
            return _ok({"message_id": self.next_id, "date": 0, "chat": {"id": int(chat)}})
        return httpx.Response(404, json={"ok": False, "description": f"not faked: {method}"})

    @property
    def texts(self) -> list[str]:
        return [str(m["text"]) for m in self.sent]


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


async def _no_sleep(_s: float) -> None:
    return None


def tg_client(fake: FakeTelegram, token: str) -> TelegramClient:
    return TelegramClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake)), token=token, sleep=_no_sleep
    )


def new_token() -> str:
    return f"{secrets.randbelow(9 * 10**9) + 10**9}:{secrets.token_urlsafe(26)}"


@dataclass
class Bot:
    channel: TenantChannel
    token: str
    secret: str

    @property
    def bot_id(self) -> str:
        assert self.channel.telegram_bot_id is not None
        return self.channel.telegram_bot_id


async def make_bot(db: Database, tenant_id: uuid.UUID, *, active: bool = True) -> Bot:
    """A connected bot, as the console leaves it: token encrypted, only the secret's hash kept."""
    token, secret = new_token(), secrets.token_urlsafe(48)
    ch = TenantChannel(
        tenant_id=tenant_id,
        kind="telegram",
        telegram_bot_id=token.split(":")[0],
        telegram_username=f"bot{token.split(':')[0]}",
        access_token_encrypted=encrypt_secret(token),
        webhook_secret_hash=hashlib.sha256(secret.encode()).digest(),
        webhook_set_at=datetime.now(UTC),
        is_active=active,
    )
    async with db.platform_session() as s:
        s.add(ch)
    return Bot(ch, token, secret)


_ids = iter(range(10**6, 10**7))


def tg_message(
    user: int,
    text: str | None = "hello",
    *,
    chat_type: str = "private",
    voice: bool = False,
    first: str = "Mona",
    username: str | None = "mona_k",
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": next(_ids),
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": user if chat_type == "private" else -100 - user, "type": chat_type},
        "from": {"id": user, "is_bot": False, "first_name": first, "username": username},
    }
    if voice:
        msg["voice"] = {"file_id": "VOICE-1", "duration": 3, "mime_type": "audio/ogg"}
    else:
        msg["text"] = text
    return msg


def update_of(**parts: Any) -> dict[str, Any]:
    return {"update_id": next(_ids), **parts}


def a_user() -> int:
    return secrets.randbelow(9 * 10**8) + 10**8


# ================================================================ formatting (no I/O)


@pytest.mark.parametrize(
    ("source", "html"),
    [
        ("*Total:* 10 AED", "<b>Total:</b> 10 AED"),
        ("_soon_ and ~old~", "<i>soon</i> and <s>old</s>"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("2 * 3 * 4", "2 * 3 * 4"),  # not markup: spaces inside the markers
        ("call snake_case_names", "call snake_case_names"),
        ("*unclosed bold", "*unclosed bold"),
        ("`a<b`", "<code>a&lt;b</code>"),
        ("```\nx = *y*\n```", "<pre>x = *y*</pre>"),
        ("<script>*hi*</script>", "&lt;script&gt;<b>hi</b>&lt;/script&gt;"),
    ],
)
def test_whatsapp_markup_becomes_telegram_html(source: str, html: str) -> None:
    assert to_telegram_html(source) == html


def test_long_text_splits_on_boundaries_and_loses_nothing() -> None:
    para = "Sentence one is here. " * 60  # ~1300 chars
    text = "\n\n".join(para.strip() for _ in range(8))  # ~10.5k chars
    parts = split_text(text)
    assert len(parts) >= 3
    assert all(len(p) <= 4000 for p in parts)
    assert " ".join(parts).split() == text.split()
    assert all(p.endswith(".") for p in parts)  # cut at paragraph/sentence ends, not mid-word
    assert split_text("x" * 9000) == ["x" * 4000, "x" * 4000, "x" * 1000]  # no boundary at all


def test_start_payloads_and_consent_refs() -> None:
    assert start_payload("/start ABCD2345") == "ABCD2345"
    assert start_payload("/start@my_bot ABCD2345") == "ABCD2345"
    assert start_payload("/start") is None
    assert start_payload("hello /start x") is None
    assert optin.find_ref("/start ABCD2345") == "ABCD2345"
    assert optin.find_ref("/start abcd2345") == "ABCD2345"
    assert optin.find_ref("/start ABCD234") is None  # wrong length: not a ref
    assert optin.find_ref("Hi (Ref ABCD2345)") == "ABCD2345"  # WhatsApp's form unchanged


def test_only_private_human_messages_are_normalised() -> None:
    user = a_user()
    m = normalise("123456", TgMessage.model_validate(tg_message(user, "hi")))
    assert m is not None
    assert (m.channel_kind, m.external_user_id, m.msg_type, m.text) == (
        "telegram", str(user), "text", "hi",
    )  # fmt: skip
    assert m.external_message_id.startswith(f"tg:123456:{user}:")
    assert (m.profile_name, m.username) == ("Mona", "mona_k")
    v = normalise("123456", TgMessage.model_validate(tg_message(user, voice=True)))
    assert v is not None
    assert (v.msg_type, v.media_ref) == ("audio", "tg-file:VOICE-1")
    assert normalise("1", TgMessage.model_validate(tg_message(user, chat_type="group"))) is None
    bot = tg_message(user)
    bot["from"]["is_bot"] = True
    assert normalise("1", TgMessage.model_validate(bot)) is None


# ================================================================ client: the token stays secret


async def test_the_bot_token_never_reaches_a_log_line_or_an_error(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeTelegram()
    token = new_token()
    client = tg_client(fake, token)
    assert (await client.get_me()).is_bot
    await client.send_message("42", "hi", parse_mode=None)
    fake.reject_token = True
    with pytest.raises(TelegramAPIError) as err:
        await client.get_me()
    assert token not in str(err.value)

    def refuse(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    broken = TelegramClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(refuse)), token=token, max_retries=1,
        sleep=_no_sleep,
    )  # fmt: skip
    with pytest.raises(TelegramAPIError) as err:
        await broken.get_me()
    assert token not in str(err.value)
    assert token not in repr(client)
    assert "HTTP Request" in caplog.text  # httpx did log the requests…
    assert token not in caplog.text  # …without the token
    assert token.split(":")[1] not in capsys.readouterr().out


async def test_429_is_retried_after_telegram_says_and_a_send_timeout_is_not() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                "description": "Too Many Requests", "parameters": {"retry_after": 3}})  # fmt: skip
        return _ok({"message_id": 7, "date": 0, "chat": {"id": 42}})

    async def sleep(s: float) -> None:
        slept.append(s)

    c = TelegramClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(flaky)), token=new_token(), sleep=sleep
    )
    assert await c.send_message("42", "hi", parse_mode=None) == 7
    assert calls["n"] == 2
    assert slept[0] >= 3

    def slow(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    calls["n"] = 0
    c = TelegramClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(slow)), token=new_token(), sleep=sleep
    )
    with pytest.raises(TelegramAPIError):
        await c.send_message("42", "hi", parse_mode=None)
    assert calls["n"] == 1  # may have been delivered: never sent twice


# ================================================================ sender


async def test_sender_splits_formats_and_falls_back_to_plain_text() -> None:
    fake = FakeTelegram()
    token = new_token()
    sender = TelegramSender(tg_client(fake, token))
    long = "\n\n".join(f"*Part {i}* " + "word " * 900 for i in range(3))
    result = await sender.send_text("42", long)
    assert len(fake.sent) == len(split_text(long)) > 1
    assert all(m["parse_mode"] == "HTML" for m in fake.sent)
    assert fake.sent[0]["text"].startswith("<b>Part 0</b>")
    assert all(m["link_preview_options"] == {"is_disabled": True} for m in fake.sent)
    assert result.wamid == f"tg:{token.split(':')[0]}:42:{fake.sent[0] and 101}"

    fake.sent.clear()
    fake.reject_html = True
    await sender.send_text("42", "*bold* text")
    assert fake.sent == [
        {"chat_id": "42", "text": "*bold* text", "link_preview_options": {"is_disabled": True}}
    ]  # resent as plain text: never lost over formatting


# ================================================================ webhook


@dataclass
class TgHarness:
    app: FastAPI
    client: httpx.AsyncClient
    jobs: RecordingEnqueuer
    db: Database

    async def post(
        self,
        bot: Bot | str,
        update: dict[str, Any] | bytes,
        *,
        secret: str | None = "auto",  # noqa: S107 — "use the bot's own secret"
    ) -> httpx.Response:
        key = bot if isinstance(bot, str) else bot.channel.channel_key
        body = update if isinstance(update, bytes) else json.dumps(update).encode()
        headers = {"content-type": "application/json"}
        if secret == "auto":
            assert isinstance(bot, Bot)
            headers["x-telegram-bot-api-secret-token"] = bot.secret
        elif secret is not None:
            headers["x-telegram-bot-api-secret-token"] = secret
        return await self.client.post(f"/webhook/telegram/{key}", content=body, headers=headers)


@pytest.fixture
async def tg(settings: Settings, migrated: None, no_network: None) -> AsyncIterator[TgHarness]:
    application = create_app()
    async with LifespanManager(application):
        jobs = RecordingEnqueuer()
        application.state.telegram_ingestor = TelegramIngestor(
            application.state.db, application.state.redis, jobs, dedup_ttl_s=3600
        )
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield TgHarness(application, client, jobs, application.state.db)


async def _rows(db: Database, tenant_id: uuid.UUID) -> dict[str, int]:
    out: dict[str, int] = {}
    async with db.tenant_session(tenant_id) as s:
        for name, model in (
            ("customers", Customer),
            ("identities", CustomerIdentity),
            ("conversations", Conversation),
            ("messages", Message),
            ("events", WebhookEvent),
        ):
            out[name] = int(await s.scalar(select(func.count()).select_from(model)) or 0)
    return out


async def test_a_verified_update_lands_under_the_bots_tenant_only(tg: TgHarness) -> None:
    a, b = await make_tenant(tg.db, "tga"), await make_tenant(tg.db, "tgb")
    bot = await make_bot(tg.db, a)
    user = a_user()
    r = await tg.post(bot, update_of(message=tg_message(user, "do you deliver today?")))
    assert r.status_code == 200, r.text
    assert await _rows(tg.db, b) == dict.fromkeys(
        ("customers", "identities", "conversations", "messages", "events"), 0
    )
    async with tg.db.tenant_session(a) as s:
        ident = await s.scalar(select(CustomerIdentity))
        assert ident is not None
        assert (ident.kind, ident.external_id, ident.username) == ("telegram", str(user), "mona_k")
        cust = await s.get(Customer, ident.customer_id)
        assert cust is not None
        assert (cust.wa_id, cust.name, cust.source) == (None, "Mona", "telegram")
        conv = await s.scalar(select(Conversation))
        assert conv is not None
        assert conv.channel_id == bot.channel.id
        assert conv.service_window_expires_at is None  # Telegram has no service window
        msg = await s.scalar(select(Message))
        assert msg is not None
        assert msg.body == "do you deliver today?"
        assert msg.wamid is not None
        assert msg.wamid.startswith(f"tg:{bot.bot_id}:{user}:")
        event = await s.scalar(select(WebhookEvent))
        assert event is not None
        assert event.channel_id == bot.channel.id
    assert [(j[0], j[2]) for j in tg.jobs.jobs] == [("handle_inbound_message", f"in:{msg.wamid}")]


async def test_a_wrong_or_missing_secret_is_refused_before_parsing(tg: TgHarness) -> None:
    a = await make_tenant(tg.db, "sec")
    bot = await make_bot(tg.db, a)
    other = await make_bot(tg.db, await make_tenant(tg.db, "sec2"))
    upd = update_of(message=tg_message(a_user()))
    assert (await tg.post(bot, upd, secret=None)).status_code == 403
    assert (await tg.post(bot, upd, secret="nope")).status_code == 403
    assert (await tg.post(bot, upd, secret=other.secret)).status_code == 403  # bot B's secret
    assert (await tg.post(bot, b"{not json", secret=None)).status_code == 403  # not even parsed
    assert (await _rows(tg.db, a))["messages"] == 0
    assert tg.jobs.jobs == []


async def test_unknown_inactive_suspended_and_whatsapp_keys_all_404(
    tg: TgHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await make_tenant(tg.db, "k404")
    off = await make_bot(tg.db, a, active=False)
    suspended_tenant = await make_tenant(tg.db, "susp")
    suspended = await make_bot(tg.db, suspended_tenant)
    async with tg.db.platform_session() as s:
        await s.execute(
            update(Tenant).where(Tenant.id == suspended_tenant).values(status="suspended")
        )
    wa = await make_channel(tg.db, a)
    upd = update_of(message=tg_message(a_user()))
    for bot in (off, suspended):
        assert (await tg.post(bot, upd)).status_code == 404
    assert (await tg.post(wa.channel_key, upd, secret="x")).status_code == 404
    assert (await tg.post(secrets.token_urlsafe(24), upd, secret="x")).status_code == 404

    async def never(_key: str) -> None:
        raise AssertionError("a malformed key must not reach Redis or Postgres")

    monkeypatch.setattr(tg.app.state.telegram_router, "resolve", never)
    for bad in ("short", "x" * 65, "bad%20chars-but-long-enough", "..%2F..%2Fetc%2Fpasswd-long"):
        assert (await tg.post(bad, upd, secret="x")).status_code == 404, bad


async def test_redelivery_is_stored_once_even_without_redis_dedup(tg: TgHarness) -> None:
    a = await make_tenant(tg.db, "dup")
    bot = await make_bot(tg.db, a)
    upd = update_of(message=tg_message(a_user()))
    assert (await tg.post(bot, upd)).status_code == 200
    assert (await tg.post(bot, upd)).status_code == 200  # Redis claim
    await tg.app.state.redis.delete(f"dedup:tg:{bot.channel.id}:{upd['update_id']}")
    assert (await tg.post(bot, upd)).status_code == 200  # Postgres UNIQUE
    assert (await _rows(tg.db, a))["messages"] == 1
    assert len(tg.jobs.jobs) == 1


async def test_two_first_messages_at_once_make_one_customer(tg: TgHarness) -> None:
    a = await make_tenant(tg.db, "race")
    bot = await make_bot(tg.db, a)
    user = a_user()
    rs = await asyncio.gather(
        *(tg.post(bot, update_of(message=tg_message(user, f"m{i}"))) for i in range(4))
    )
    assert [r.status_code for r in rs] == [200] * 4
    rows = await _rows(tg.db, a)
    assert (rows["customers"], rows["identities"], rows["conversations"], rows["messages"]) == (
        1, 1, 1, 4,
    )  # fmt: skip


async def test_groups_edits_and_other_updates_are_ignored(tg: TgHarness) -> None:
    a = await make_tenant(tg.db, "ign")
    bot = await make_bot(tg.db, a)
    user = a_user()
    for upd in (
        update_of(message=tg_message(user, chat_type="group")),
        update_of(edited_message=tg_message(user)),
        update_of(channel_post={"message_id": 1, "date": 0, "chat": {"id": -5, "type": "channel"}}),
        update_of(callback_query={"id": "1"}),
    ):
        assert (await tg.post(bot, upd)).status_code == 200
    assert (await tg.post(bot, b'{"update_id": "not a number"}')).status_code == 200  # dropped
    assert await _rows(tg.db, a) == dict.fromkeys(
        ("customers", "identities", "conversations", "messages", "events"), 0
    )


async def test_blocking_and_unblocking_the_bot_is_recorded(tg: TgHarness) -> None:
    a = await make_tenant(tg.db, "blk")
    bot = await make_bot(tg.db, a)
    user = a_user()
    await tg.post(bot, update_of(message=tg_message(user)))

    def member(status: str) -> dict[str, Any]:
        return update_of(
            my_chat_member={
                "chat": {"id": user, "type": "private"},
                "from": {"id": user, "is_bot": False, "first_name": "Mona"},
                "date": 0,
                "old_chat_member": {"status": "member"},
                "new_chat_member": {"status": status},
            }
        )

    async def blocked_at() -> datetime | None:
        async with tg.db.tenant_session(a) as s:
            return await s.scalar(select(CustomerIdentity.blocked_at))

    assert (await tg.post(bot, member("kicked"))).status_code == 200
    assert await blocked_at() is not None
    async with tg.db.tenant_session(a) as s:
        cust = await s.scalar(select(Customer))
        assert cust is not None
        assert cust.opt_in_status == "pending"  # blocking is not an opt-out
    assert (await tg.post(bot, member("member"))).status_code == 200
    assert await blocked_at() is None


async def test_a_database_failure_answers_503_and_the_redelivery_is_stored(
    tg: TgHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await make_tenant(tg.db, "dbdown")
    bot = await make_bot(tg.db, a)
    upd = update_of(message=tg_message(a_user()))
    real = inbound.persist_inbound

    async def down(*_a: Any, **_k: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr("api.channels.telegram.ingest.persist_inbound", down)
    assert (await tg.post(bot, upd)).status_code == 503  # Telegram keeps it and retries
    monkeypatch.setattr("api.channels.telegram.ingest.persist_inbound", real)
    assert (await tg.post(bot, upd)).status_code == 200  # the claim was released
    assert (await _rows(tg.db, a))["messages"] == 1


async def test_secrets_never_reach_the_logs(
    tg: TgHarness, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    a = await make_tenant(tg.db, "logs")
    bot = await make_bot(tg.db, a)
    await tg.post(bot, update_of(message=tg_message(a_user(), "my secret order")))
    await tg.post(bot, update_of(message=tg_message(a_user())), secret="wrong")
    # everything the app logged (stdout JSON + stdlib); the test client's own httpx log of the
    # URL it called is not the app's
    out = capsys.readouterr().out + "\n".join(
        r.getMessage() for r in caplog.records if r.name != "httpx"
    )
    for value in (bot.secret, bot.token, bot.channel.channel_key, "my secret order"):
        assert value not in out


async def test_customer_identities_are_isolated_by_rls(db: Database) -> None:
    a, b = await make_tenant(db, "rlsa"), await make_tenant(db, "rlsb")
    async with db.tenant_session(a) as s:
        c = Customer(name="A only")
        s.add(c)
        await s.flush()
        s.add(CustomerIdentity(customer_id=c.id, kind="telegram", external_id="77"))
    async with db.tenant_session(b) as s:
        assert await s.scalar(select(func.count()).select_from(CustomerIdentity)) == 0
        # the same Telegram user can be a customer of two businesses, separately
        c2 = Customer(name="B too")
        s.add(c2)
        await s.flush()
        s.add(CustomerIdentity(customer_id=c2.id, kind="telegram", external_id="77"))


async def test_the_wa_id_trigger_keeps_whatsapp_identities_in_step(db: Database) -> None:
    t = await make_tenant(db, "trig")
    async with db.tenant_session(t) as s:
        c = Customer(wa_id="971500000111", name="X")
        s.add(c)
        await s.flush()
        cid = c.id

    async def ident() -> list[tuple[str, str]]:
        async with db.tenant_session(t) as s:
            rows = await s.execute(
                select(CustomerIdentity.kind, CustomerIdentity.external_id).where(
                    CustomerIdentity.customer_id == cid
                )
            )
            return [(k, e) for k, e in rows.all()]

    assert await ident() == [("whatsapp", "971500000111")]
    async with db.tenant_session(t) as s:
        await s.execute(update(Customer).where(Customer.id == cid).values(wa_id="971500000222"))
    assert await ident() == [("whatsapp", "971500000222")]
    async with db.tenant_session(t) as s:
        await s.execute(update(Customer).where(Customer.id == cid).values(wa_id=None))
    assert await ident() == []


# ================================================================ sending on a bot


async def _bot_conversation(db: Database, tenant: uuid.UUID, bot: Bot, user: str) -> uuid.UUID:
    async with db.tenant_session(tenant) as s:
        c = Customer(name="Mona", source="telegram")
        s.add(c)
        await s.flush()
        s.add(CustomerIdentity(customer_id=c.id, kind="telegram", external_id=user))
        conv = Conversation(customer_id=c.id, channel_id=bot.channel.id, last_inbound_at=CLOCK)
        s.add(conv)
        await s.flush()
        return conv.id


async def test_a_reply_on_a_bot_has_no_window_no_price_and_is_metered(db: Database) -> None:
    t = await make_tenant(db, "send")
    bot = await make_bot(db, t)
    user = str(a_user())
    conv = await _bot_conversation(db, t, bot, user)  # last message long ago: no window applies
    fake = FakeTelegram()
    mid = await send_text_reply(
        db, TelegramSender(tg_client(fake, bot.token)), tenant_id=t, conversation_id=conv,
        text="Hello *Mona*",
    )  # fmt: skip
    assert fake.sent[0]["chat_id"] == user
    assert fake.sent[0]["text"] == "Hello <b>Mona</b>"
    async with db.tenant_session(t) as s:
        msg = await s.get(Message, mid)
        assert msg is not None
        assert (msg.cost_aed, msg.pricing_category, msg.body) == (
            Decimal(0), "service", "Hello *Mona*",
        )  # fmt: skip
        assert msg.wamid is not None
        assert msg.wamid.startswith(f"tg:{bot.bot_id}:{user}:")
        usage = await s.get(UsageDaily, (t, usage_day()))
        assert usage is not None
        assert usage.msgs_out == 1


async def test_a_blocked_bot_marks_the_customer_unreachable(db: Database) -> None:
    t = await make_tenant(db, "blocked")
    bot = await make_bot(db, t)
    user = str(a_user())
    conv = await _bot_conversation(db, t, bot, user)
    fake = FakeTelegram(blocked={user})
    sender = TelegramSender(tg_client(fake, bot.token))
    with pytest.raises(RecipientUnreachableError):
        await send_text_reply(db, sender, tenant_id=t, conversation_id=conv, text="hi")
    async with db.tenant_session(t) as s:
        assert await s.scalar(select(CustomerIdentity.blocked_at)) is not None
        assert await s.scalar(select(func.count()).select_from(Message)) == 0
    fake.blocked.clear()
    with pytest.raises(RecipientUnreachableError):  # not even tried until they write again
        await send_text_reply(db, sender, tenant_id=t, conversation_id=conv, text="hi")
    assert fake.sent == []


# ================================================================ the agent on a bot


# a new contact's first reply must carry the AI disclosure (the validator insists)
HELLO = "Hello! I'm Sara, Test Water Co's automated assistant — our team is here too."


@dataclass
class BotShop:
    shop: Shop
    bot: Bot
    fake: FakeTelegram
    ingestor: TelegramIngestor

    async def receive(self, user: int, text: str | None = None, *, voice: bool = False) -> None:
        route = await TelegramRouter(self.shop.db, self.shop.redis).resolve(
            self.bot.channel.channel_key
        )
        assert route is not None
        await self.ingestor.ingest(
            route, Update.model_validate(update_of(message=tg_message(user, text, voice=voice)))
        )

    def runner(self, llm: Any, transcriber: Any = None) -> TurnRunner:
        tg_http = httpx.AsyncClient(transport=httpx.MockTransport(self.fake))
        base = self.shop.runner(llm, transcriber)

        def senders(ch: TenantChannel) -> Any:
            if ch.kind == "telegram":
                return TelegramSender(TelegramClient(http=tg_http, token=self.bot.token))
            return base._client_factory(ch)

        base._sender_factory = senders
        return base

    async def run_turns(self, llm: Any, transcriber: Any = None) -> list[str]:
        runner = self.runner(llm, transcriber)
        pending = [j for j in self.shop.jobs.jobs if j[0] == "handle_inbound_message"]
        self.shop.jobs.jobs = [j for j in self.shop.jobs.jobs if j[0] != "handle_inbound_message"]
        return [await runner.handle(uuid.UUID(a[0]), uuid.UUID(a[1])) for _, a, _ in pending]

    async def customer(self, user: int) -> Customer:
        async with self.shop.db.tenant_session(self.shop.tenant_id) as s:
            c = await s.scalar(
                select(Customer)
                .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
                .where(
                    CustomerIdentity.kind == "telegram", CustomerIdentity.external_id == str(user)
                )
            )
            assert c is not None
            return c


@pytest.fixture
async def bot_shop(shop: Shop) -> BotShop:  # noqa: F811 — the imported fixture
    bot = await make_bot(shop.db, shop.tenant_id)
    ingestor = TelegramIngestor(shop.db, shop.redis, shop.jobs, dedup_ttl_s=3600)
    return BotShop(shop, bot, FakeTelegram(), ingestor)


async def test_the_agent_answers_on_telegram_and_says_so(bot_shop: BotShop) -> None:
    user = a_user()
    llm = ScriptedLLM("support", "en", [say(f"{HELLO} We deliver every day. Anything else?")])
    await bot_shop.receive(user, "when do you deliver?")
    assert await bot_shop.run_turns(llm) == ["replied"]
    assert "We deliver every day" in bot_shop.fake.texts[-1]
    assert bot_shop.fake.actions == [{"chat_id": str(user), "action": "typing"}]
    system = next(c for c in llm.calls if not c.get("json_mode"))["messages"][0]["content"]
    assert "Telegram" in system
    assert "WhatsApp" not in system
    async with bot_shop.shop.db.tenant_session(bot_shop.shop.tenant_id) as s:
        out = await s.scalar(
            select(Message).where(Message.direction == "out", Message.wamid.like("tg:%"))
        )
        assert out is not None
        assert (out.prompt_version or "").endswith("@telegram")
        assert out.cost_aed == Decimal(0)
    assert bot_shop.shop.meta.sent == []  # nothing went to WhatsApp


async def test_a_telegram_voice_note_is_transcribed(bot_shop: BotShop) -> None:
    user = a_user()
    llm = ScriptedLLM("support", "en", [say(f"{HELLO} Yes, we deliver to Al Nahda.")])
    await bot_shop.receive(user, voice=True)
    await bot_shop.run_turns(llm, FakeTranscriber("do you deliver to al nahda"))
    async with bot_shop.shop.db.tenant_session(bot_shop.shop.tenant_id) as s:
        m = await s.scalar(select(Message).where(Message.msg_type == "audio"))
        assert m is not None
        assert (m.media_url, m.transcript) == ("tg-file:VOICE-1", "do you deliver to al nahda")


@pytest.mark.parametrize("text", ["STOP", "/stop"])
async def test_stop_on_telegram_opts_out_without_a_model_call(bot_shop: BotShop, text: str) -> None:
    user = a_user()
    llm = ScriptedLLM("unused", "en", [])
    await bot_shop.receive(user, text)
    assert await bot_shop.run_turns(llm) == ["opted_out"]
    assert llm.calls == []
    assert (await bot_shop.customer(user)).opt_in_status == "opted_out"
    assert bot_shop.fake.texts  # one confirmation


async def test_start_with_a_consent_ref_opts_in_with_evidence(bot_shop: BotShop) -> None:
    shop_ = bot_shop.shop
    async with shop_.db.platform_session() as s:
        link = OptinLink(
            tenant_id=shop_.tenant_id, code=optin.new_code(), label="Van 3", source="qr_van",
            heading="Offers on Telegram", wording="Yes, send me offers and updates on Telegram.",
            prefill="Yes please",
        )  # fmt: skip
        s.add(link)
    async with shop_.db.tenant_session(shop_.tenant_id) as s:
        visit = await optin.record_visit(s, link)
        ref = visit.token
    user = a_user()
    llm = ScriptedLLM("unused", "en", [])
    await bot_shop.receive(user, f"/start {ref}")
    assert await bot_shop.run_turns(llm) == ["opted_in"]
    assert llm.calls == []
    c = await bot_shop.customer(user)
    assert (c.opt_in_status, c.source) == ("opted_in", "qr_van")
    ev = c.opt_in_evidence or {}
    assert ev["wording_shown"] == "Yes, send me offers and updates on Telegram."
    assert str(ev["wamid"]).startswith("tg:")


# ================================================================ consent page


async def test_the_consent_page_offers_telegram_and_redirects_to_the_bot(
    tg: TgHarness,
) -> None:
    t = await make_tenant(tg.db, "page")
    bot = await make_bot(tg.db, t)
    async with tg.db.platform_session() as s:
        link = OptinLink(
            tenant_id=t, code=optin.new_code(), label="Shop", source="qr_shop",
            heading="Offers", wording="Yes, send me offers and updates please.", prefill="Hi",
        )  # fmt: skip
        s.add(link)
    page = await tg.client.get(f"/q/{link.code}")
    assert page.status_code == 200
    assert "Continue on Telegram" in page.text
    assert "Continue on WhatsApp" not in page.text  # this client has no number
    assert "https://t.me" in page.headers["content-security-policy"]
    r = await tg.client.post(f"/q/{link.code}", data={"via": "telegram"})
    assert r.status_code == 303
    target = httpx.URL(r.headers["location"])
    assert (target.host, target.path) == ("t.me", f"/{bot.channel.telegram_username}")
    assert optin.find_ref(f"/start {target.params['start']}") is not None


# ================================================================ campaigns on a bot


async def test_a_telegram_campaign_sends_free_text_to_reachable_opted_in_customers(
    db: Database, settings: Settings
) -> None:
    t = await make_tenant(db, "tgcamp", modules=WATER)
    bot = await make_bot(db, t)
    users: dict[str, str] = {}
    async with db.tenant_session(t) as s:
        for who, status, blocked in (
            ("ok1", "opted_in", False),
            ("ok2", "opted_in", False),
            ("blocked", "opted_in", True),
            ("pending", "pending", False),
        ):
            c = Customer(name=f"{who.title()} X", opt_in_status=status, area="Al Nahda")
            s.add(c)
            await s.flush()
            users[who] = str(a_user())
            s.add(
                CustomerIdentity(
                    customer_id=c.id, kind="telegram", external_id=users[who],
                    blocked_at=CLOCK if blocked else None,
                )
            )  # fmt: skip
        # opted in, but only on WhatsApp: a Telegram campaign cannot reach them
        s.add(Customer(wa_id="971500000777", name="Wa Only", opt_in_status="opted_in"))
        camp = Campaign(
            name="Bot offer",
            channel_kind="telegram",
            body="Hi {{1}}! Fresh water in {{2}} this week.",
            segment_query={"opt_in_status": "opted_in"},
            variable_bindings=BINDINGS,
        )
        s.add(camp)
        await s.flush()
        service.approve(s, camp, uuid.uuid4(), "user:test", CLOCK)
        n = await service.start(s, camp, enabled_of(WATER), scope(), "user:test")
        cid = camp.id
    assert n == 2  # the blocked, pending and WhatsApp-only customers are not snapshotted

    fake = FakeTelegram()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    ctx = {
        "db": db, "settings": settings, "http": http, "redis": FakeRedis(), "clock": lambda: CLOCK,
        "sender_factory": lambda ch: TelegramSender(TelegramClient(http=http, token=bot.token)),
    }  # fmt: skip
    report = await run_campaign(ctx, str(t), str(cid))
    assert (report["sent"], report["failed"]) == (2, 0)
    assert sorted(m["chat_id"] for m in fake.sent) == sorted([users["ok1"], users["ok2"]])
    assert "Fresh water in Al Nahda this week." in fake.texts[0]
    async with db.tenant_session(t) as s:
        got = await s.get(Campaign, cid)
        assert got is not None
        assert (got.sent_count, got.spend_aed) == (2, Decimal(0))
        cats = (await s.scalars(select(Message.pricing_category))).all()
        assert set(cats) == {"marketing"}
    assert (await run_campaign(ctx, str(t), str(cid)))["status"] == "done"


async def test_a_telegram_campaign_pauses_when_too_many_have_blocked_the_bot(
    db: Database, settings: Settings
) -> None:
    t = await make_tenant(db, "tgblock", modules=WATER)
    bot = await make_bot(db, t)
    blocked: set[str] = set()
    async with db.tenant_session(t) as s:
        for i in range(40):
            c = Customer(name=f"P{i}", opt_in_status="opted_in")
            s.add(c)
            await s.flush()
            uid = str(a_user())
            if i % 5 == 0:  # 20 % have blocked the bot since they last wrote
                blocked.add(uid)
            s.add(CustomerIdentity(customer_id=c.id, kind="telegram", external_id=uid))
        camp = Campaign(
            name="Too pushy", channel_kind="telegram", body="Hello!",
            segment_query={"opt_in_status": "opted_in"}, variable_bindings=[],
            throttle_per_minute=100,
        )  # fmt: skip
        s.add(camp)
        await s.flush()
        service.approve(s, camp, uuid.uuid4(), "user:test", CLOCK)
        await service.start(s, camp, enabled_of(WATER), scope(), "user:test")
        cid = camp.id
    fake = FakeTelegram(blocked=blocked)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    ctx = {
        "db": db, "settings": settings, "http": http, "redis": FakeRedis(), "clock": lambda: CLOCK,
        "sender_factory": lambda ch: TelegramSender(TelegramClient(http=http, token=bot.token)),
    }  # fmt: skip
    report = await run_campaign(ctx, str(t), str(cid))
    assert report["status"] == "paused"
    async with db.tenant_session(t) as s:
        got = await s.get(Campaign, cid)
        assert got is not None
        assert got.paused_reason == "block_rate"
        pending = await s.scalar(
            select(func.count()).where(
                CampaignRecipient.campaign_id == cid, CampaignRecipient.status == "pending"
            )
        )
        assert pending  # stopped before reaching everyone
        assert await s.scalar(select(func.count()).where(CustomerIdentity.blocked_at.is_not(None)))


async def test_templates_and_text_bodies_stay_on_their_own_channels(db: Database) -> None:
    t = await make_tenant(db, "tgready", modules=WATER)
    async with db.tenant_session(t) as s:
        c = Campaign(name="x", channel_kind="telegram", segment_query={"opt_in_status": "opted_in"})
        s.add(c)
        await s.flush()
        with pytest.raises(service.CampaignError) as err:
            await service.ready(s, c)
        assert err.value.code == "no_body"
        c.body = "Hi {{1}}"
        with pytest.raises(service.CampaignError) as err:
            await service.ready(s, c)
        assert err.value.code == "bindings_mismatch"
        c.variable_bindings = BINDINGS[:1]
        await service.ready(s, c)


# ================================================================ the today screen


async def test_window_semantics_follow_the_channel(db: Database) -> None:
    """A Telegram conversation counts as live for 24 h after the customer wrote, with no window."""
    t = await make_tenant(db, "live")
    bot = await make_bot(db, t)
    conv = await _bot_conversation(db, t, bot, str(a_user()))
    async with db.tenant_session(t) as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conv)
            .values(last_inbound_at=datetime.now(UTC) - timedelta(hours=1))
        )
        live = await s.scalar(
            select(func.count()).where(
                (Conversation.service_window_expires_at > func.now())
                | (
                    Conversation.service_window_expires_at.is_(None)
                    & (Conversation.last_inbound_at > func.now() - timedelta(hours=24))
                )
            )
        )
    assert live == 1
