"""One inbound message → at most one reply. Worker-side orchestration (01 §6.1 steps 7-15).

 1  load the message under its tenant; voice note → download + transcribe → store transcript
 2  explicit STOP → opt out + confirmation, NO model call (absolute; runs even mid-handover)
    consent-page ref ("… (Ref ABCD2345)") → opt in with evidence + confirmation, NO model call
 3  debounce: the job is deferred at ingest (turn_debounce_s); if a newer inbound message
    exists by the time it runs, stop — that message's job answers both
 4  per-conversation Redis lock (one turn at a time per conversation)
 5  conversation awaiting_human → store only, no agent reply
 6  classify (Flash-Lite): optout / complaint short-circuit, smalltalk canned, else Support Agent
 7  send via Meta with cost stamped + usage metered (api.meta.outbound)
 8  enqueue escalation alert; every Nth inbound → enqueue rolling summary

LLM both down → holding message once, then the job is retried later (queue the turn).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select

from api.agents import prompts
from api.agents.canned import Kind, canned
from api.agents.classifier import classify, explicit_optout, guess_language
from api.agents.context import (
    Persona,
    is_new_conversation,
    load_history,
    load_persona,
    mark_handled,
    pending_inbound,
    render_customer_block,
    render_support_prompt,
    retrieve_knowledge,
)
from api.agents.prompts import compose
from api.agents.support import SupportAgent, SupportTurn
from api.agents.tools.base import Escalation, EscalationReason, ToolContext, escalate
from api.agents.tools.record_opt_out import mark_opted_out
from api.channels.base import ChannelSender, caps_for
from api.channels.errors import ChannelAPIError, RecipientUnreachableError
from api.channels.outbound import (
    ChannelNotConfiguredError,
    LLMUsage,
    recipient_for,
    send_text_reply,
)
from api.config import Settings
from api.core.logging import get_logger
from api.db.models import Conversation, Customer, Message, Tenant, TenantChannel
from api.db.session import Database
from api.llm.embeddings import Embedder
from api.llm.router import LLM, LLMResult, LLMUnavailableError
from api.llm.transcribe import Transcriber
from api.meta.client import MetaClient
from api.metering import record_usage
from api.optin import service as optin
from api.optin.service import find_ref
from api.webhooks.ingest import Enqueuer

log = get_logger(__name__)

Outcome = Literal[
    "replied",
    "opted_out",
    "opted_in",
    "no_claim",
    "escalated",
    "suppressed",
    "superseded",
    "answered",
    "locked",
    "llm_down",
    "missing",
    "ignored",
]

JOB_SUMMARISE: Final = "summarise_conversation"
JOB_NOTIFY: Final = "notify_escalation"

_RELEASE_LUA: Final = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)
_THANKS = re.compile(r"\b(thanks?|thank you|thx|ty|shukran|merci)\b|شكر", re.I)

# complaint → escalation reason (02 §3 enum). Deterministic, first match wins.
_COMPLAINT_REASONS: Final[tuple[tuple[re.Pattern[str], EscalationReason], ...]] = (
    (
        re.compile(
            r"broken|damaged|leak|crack|dirty|smell|expired|مكسور|تالف|مكسورة|يسرب|وسخ", re.I
        ),
        "damaged_goods",
    ),
    (re.compile(r"driver|delivery guy|rude|السائق|المندوب|سواق", re.I), "driver_issue"),
    (
        re.compile(
            r"refund|money back|charged|overcharg|bill|discount|% ?off|cancel|"
            r"استرداد|فلوس|خصم|فاتورة",
            re.I,
        ),
        "refund_or_billing",
    ),
    (
        re.compile(r"human|person|agent|manager|someone real|موظف|شخص|مدير", re.I),
        "customer_asked_for_human",
    ),
)

ClientFactory = Callable[[TenantChannel], MetaClient]
SenderFactory = Callable[[TenantChannel], ChannelSender]
# where an inbound message's media lives, by channel: "<prefix><id>" in messages.media_url
MEDIA_PREFIXES: Final = ("meta-media:", "tg-file:")


def complaint_reason(text: str) -> EscalationReason:
    for pattern, reason in _COMPLAINT_REASONS:
        if pattern.search(text):
            return reason
    return "complaint"


@dataclass
class TurnMeter:
    calls: list[tuple[str, int, int, int, Decimal]] = field(
        default_factory=list
    )  # model, pt, ct, ms, usd
    recorded: bool = False

    def add(self, r: LLMResult) -> None:
        self.calls.append((r.model, r.prompt_tokens, r.completion_tokens, r.latency_ms, r.cost_usd))

    def add_raw(self, model: str, pt: int, ct: int, ms: int, usd: Decimal) -> None:
        self.calls.append((model, pt, ct, ms, usd))

    def usage(self, prompt_version: str | None) -> LLMUsage | None:
        if not self.calls:
            return None
        return LLMUsage(
            model=self.calls[-1][0],
            prompt_tokens=sum(c[1] for c in self.calls),
            completion_tokens=sum(c[2] for c in self.calls),
            latency_ms=sum(c[3] for c in self.calls),
            prompt_version=prompt_version,
            cost_usd=sum((c[4] for c in self.calls), Decimal(0)),
        )


class TurnRunner:
    def __init__(
        self,
        *,
        db: Database,
        redis: Redis,
        llm: LLM,
        settings: Settings,
        client_factory: ClientFactory,
        sender_factory: SenderFactory | None = None,
        embedder: Embedder | None = None,
        transcriber: Transcriber | None = None,
        enqueuer: Enqueuer | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._db = db
        self._redis = redis
        self._llm = llm
        self._settings = settings
        self._client_factory = client_factory
        self._sender_factory = sender_factory
        self._embedder = embedder
        self._transcriber = transcriber
        self._enqueuer = enqueuer
        self._sleep = sleep
        self._support = SupportAgent(db, llm, max_rounds=settings.support_max_tool_rounds)

    # ================================================================ entry point

    async def handle(
        self, tenant_id: uuid.UUID, message_id: uuid.UUID, *, job_try: int = 1
    ) -> Outcome:
        log_ctx = {"tenant_id": str(tenant_id)}
        async with self._db.tenant_session(tenant_id) as s:
            msg = await s.get(Message, message_id)
            if msg is None:
                log.error("turn_message_missing", **log_ctx)
                return "missing"
            if msg.direction != "in":
                return "ignored"
            conv = await s.get(Conversation, msg.conversation_id)
            if conv is None:
                return "missing"
            conversation_id, customer_id, channel_id = conv.id, conv.customer_id, conv.channel_id
            msg_created, wamid, already = msg.created_at, msg.wamid, msg.status == "handled"
        log_ctx["wamid"] = wamid or ""
        if already:
            return "answered"

        channel = await self._channel(channel_id)
        meter = TurnMeter()
        text = await self._message_text(tenant_id, message_id, channel, meter)

        # 2. explicit STOP — deterministic, before anything that could delay or suppress it
        if text and explicit_optout(text):
            persona = await load_persona(self._db, tenant_id)
            return await self._locked(
                conversation_id,
                lambda: self._stop(
                    persona, channel, conversation_id, customer_id, message_id, wamid, text, meter
                ),
                job_try,
            )

        # 2b. the prefilled message from a consent page (api/optin) — deterministic too
        if text and find_ref(text):
            outcome = await self._locked(
                conversation_id,
                lambda: self._consent(
                    tenant_id, channel, conversation_id, customer_id, message_id, wamid, text, meter
                ),
                job_try,
            )
            if outcome != "no_claim":
                return outcome

        # 3. debounce — the job was deferred by turn_debounce_s at ingest, so it starts only after
        #    the pause (without holding a worker slot while it waits) — then give way to a newer
        #    message, whose own job will answer both
        async with self._db.tenant_session(tenant_id) as s:
            newer = await s.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "in",
                    Message.created_at > msg_created,
                )
            )
        if newer:
            log.info("turn_superseded", **log_ctx)
            return "superseded"

        return await self._locked(
            conversation_id,
            lambda: self._turn(
                tenant_id, conversation_id, customer_id, channel, wamid, meter, job_try
            ),
            job_try,
        )

    # ================================================================ the turn

    async def _turn(
        self,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel: TenantChannel,
        wamid: str | None,
        meter: TurnMeter,
        job_try: int,
    ) -> Outcome:
        persona = await load_persona(self._db, tenant_id)
        now = datetime.now(UTC)
        today = persona.today(now)

        async with self._db.tenant_session(tenant_id) as s:
            conv = await s.get(Conversation, conversation_id)
            customer = await s.get(Customer, customer_id)
            if conv is None or customer is None:
                return "missing"
            if conv.state == "awaiting_human":
                log.info(
                    "turn_suppressed_awaiting_human", tenant_id=str(tenant_id), wamid=wamid or ""
                )
                await self._flush_meter(tenant_id, meter)
                return "suppressed"
            pending = await pending_inbound(s, conversation_id)
            if not pending:
                return "answered"
            pending_ids = [m.id for m in pending]
            customer_text = "\n".join(
                (m.transcript or m.body or f"[{m.msg_type}]") for m in pending
            )
            new_conversation = await is_new_conversation(s, conv)
            summary = conv.summary
            known_language = conv.language or customer.language
            # is this a reply to something a module sent (a campaign)? counted here, whatever
            # the intent turns out to be; the context goes to the support prompt below
            reply_blocks: list[str] = []
            for module in persona.enabled.modules:
                if module.reply_context is not None:
                    text = await module.reply_context(
                        s, customer_id, now, persona.enabled.config(module.key)
                    )
                    if text:
                        reply_blocks.append(text)

        # 6. classify
        try:
            cls = await classify(
                self._llm,
                provider=persona.llm_provider,
                model=persona.classify_model,
                text=customer_text,
                modules=persona.enabled.keys,
            )
        except LLMUnavailableError:
            return await self._llm_down(
                persona,
                channel,
                conversation_id,
                known_language or guess_language(customer_text),
                new_conversation,
                meter,
                job_try,
            )
        if cls.llm is not None:
            meter.add(cls.llm)
        language = cls.language
        log.info(
            "turn_classified",
            tenant_id=str(tenant_id),
            wamid=wamid or "",
            intent=cls.intent,
            language=language,
        )

        async with self._db.tenant_session(tenant_id) as s:
            conv = await s.get(Conversation, conversation_id)
            customer = await s.get(Customer, customer_id)
            if conv is not None:
                conv.language = language
            if customer is not None and not customer.language:
                customer.language = language

        if cls.intent == "optout":
            result = await self._opt_out(
                persona, channel, conversation_id, customer_id, wamid, language, meter
            )
            await self._handled(tenant_id, pending_ids)
            return result
        if cls.intent == "complaint":
            reason = complaint_reason(customer_text)
            async with self._db.tenant_session(tenant_id) as s:
                esc = await escalate(
                    s,
                    conversation_id,
                    reason=reason,
                    summary=f"Customer complaint ({reason.replace('_', ' ')}). "
                    "See the latest messages.",
                    urgency="high" if reason in ("damaged_goods", "driver_issue") else "normal",
                )
            await self._send_canned(
                persona,
                channel,
                conversation_id,
                "complaint_ack",
                language,
                new_conversation,
                meter,
            )
            await self._handled(tenant_id, pending_ids)
            await self._notify(tenant_id, esc)
            return "escalated"
        if cls.intent == "smalltalk":
            kind: Kind = (
                "greeting"
                if new_conversation or not _THANKS.search(customer_text)
                else "smalltalk_ack"
            )
            await self._send_canned(
                persona, channel, conversation_id, kind, language, new_conversation, meter
            )
            await self._handled(tenant_id, pending_ids)
            await self._maybe_summarise(tenant_id, conversation_id)
            return "replied"

        # Support Agent
        await self._typing(channel, conversation_id)
        async with self._db.tenant_session(tenant_id) as s:
            customer = await s.get(Customer, customer_id)
            assert customer is not None
            customer_block = await render_customer_block(s, customer, today, persona.enabled)
            if reply_blocks:
                customer_block = "\n\n".join([customer_block, *reply_blocks])
            history, previous_out = await load_history(s, conversation_id)
            knowledge = await retrieve_knowledge(s, self._embedder, customer_text)
        system_prompt, chunks_used, prompt_tokens_est = render_support_prompt(
            persona,
            customer_block=customer_block,
            knowledge=knowledge,
            now=now,
            token_cap=self._settings.system_prompt_token_cap,
            channel_name=caps_for(channel.kind).name,
        )
        log.info(
            "support_prompt",
            tenant_id=str(tenant_id),
            est_tokens=prompt_tokens_est,
            rag_chunks=chunks_used,
        )

        try:
            outcome = await self._support.reply(
                SupportTurn(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    inbound_wamid=wamid,
                    customer_text=customer_text,
                    language=language,
                    is_new_conversation=new_conversation,
                    persona=persona,
                    system_prompt=system_prompt,
                    customer_block=customer_block,
                    summary=summary,
                    history=history,
                    previous_outbound=previous_out,
                    now=now,
                    today=today,
                    other_tenant_names=await self._other_tenant_names(
                        tenant_id, persona.business_name
                    ),
                )
            )
        except LLMUnavailableError:
            return await self._llm_down(
                persona, channel, conversation_id, language, new_conversation, meter, job_try
            )
        for call in outcome.llm_calls:
            meter.add(call)

        await self._send(
            channel,
            tenant_id,
            conversation_id,
            outcome.text,
            meter,
            compose.version(persona.enabled.keys, channel.kind)
            if outcome.kind == "model"
            else prompts.CANNED,
        )
        await self._handled(tenant_id, pending_ids)
        for esc in outcome.escalations:
            await self._notify(tenant_id, esc)
        await self._maybe_summarise(tenant_id, conversation_id)
        return "escalated" if outcome.escalations else "replied"

    # ================================================================ deterministic paths

    async def _opt_out(
        self,
        persona: Persona,
        channel: TenantChannel,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        wamid: str | None,
        language: str,
        meter: TurnMeter,
    ) -> Outcome:
        tenant_id = persona.tenant_id
        async with self._db.tenant_session(tenant_id) as s:
            now = datetime.now(UTC)
            ctx = ToolContext(
                session=s,
                tenant_id=tenant_id,
                customer_id=customer_id,
                conversation_id=conversation_id,
                inbound_wamid=wamid,
                now=now,
                today=persona.today(now),
                timezone=persona.timezone,
            )
            await mark_opted_out(ctx, "stop_keyword" if not meter.calls else "classifier")
            conv = await s.get(Conversation, conversation_id)
            new_conversation = conv is not None and await is_new_conversation(s, conv)
        log.info(
            "customer_opted_out",
            tenant_id=str(tenant_id),
            wamid=wamid or "",
            llm_calls=len(meter.calls),
        )
        await self._send_canned(
            persona, channel, conversation_id, "optout_confirmed", language, new_conversation, meter
        )
        return "opted_out"

    async def _stop(
        self,
        persona: Persona,
        channel: TenantChannel,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        message_id: uuid.UUID,
        wamid: str | None,
        text: str,
        meter: TurnMeter,
    ) -> Outcome:
        result = await self._opt_out(
            persona, channel, conversation_id, customer_id, wamid, guess_language(text), meter
        )
        await self._handled(persona.tenant_id, [message_id])
        return result

    async def _consent(
        self,
        tenant_id: uuid.UUID,
        channel: TenantChannel,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        message_id: uuid.UUID,
        wamid: str | None,
        text: str,
        meter: TurnMeter,
    ) -> Outcome:
        now = datetime.now(UTC)
        async with self._db.tenant_session(tenant_id) as s:
            got = await optin.claim(s, text=text, customer_id=customer_id, wamid=wamid, now=now)
            if got is None:
                return "no_claim"  # unknown, used or expired ref: an ordinary message
            conv = await s.get(Conversation, conversation_id)
            new_conversation = conv is not None and await is_new_conversation(s, conv)
        persona = await load_persona(self._db, tenant_id)
        log.info(
            "customer_opted_in",
            tenant_id=str(tenant_id),
            wamid=wamid or "",
            method="consent_page",
            already=got.already,
        )
        await self._send_canned(
            persona, channel, conversation_id, "optin_confirmed", got.language, new_conversation,
            meter,
        )  # fmt: skip
        await self._handled(tenant_id, [message_id])
        return "opted_in"

    async def _handled(self, tenant_id: uuid.UUID, message_ids: list[uuid.UUID]) -> None:
        async with self._db.tenant_session(tenant_id) as s:
            await mark_handled(s, message_ids)

    async def _llm_down(
        self,
        persona: Persona,
        channel: TenantChannel,
        conversation_id: uuid.UUID,
        language: str,
        new_conversation: bool,
        meter: TurnMeter,
        job_try: int,
    ) -> Outcome:
        log.error("turn_llm_unavailable", tenant_id=str(persona.tenant_id), job_try=job_try)
        if job_try == 1:
            await self._send_canned(
                persona, channel, conversation_id, "holding", language, new_conversation, meter
            )
        else:
            await self._flush_meter(persona.tenant_id, meter)
        return "llm_down"

    async def escalate_after_retries(self, tenant_id: uuid.UUID, message_id: uuid.UUID) -> None:
        """Called by the job wrapper when the LLM stayed down for every retry."""
        async with self._db.tenant_session(tenant_id) as s:
            msg = await s.get(Message, message_id)
            if msg is None:
                return
            esc = await escalate(
                s,
                msg.conversation_id,
                reason="agent_unsure",
                summary="Automatic handover: the assistant was unavailable and could not answer.",
                urgency="high",
            )
        await self._notify(tenant_id, esc)

    # ================================================================ plumbing

    async def _message_text(
        self, tenant_id: uuid.UUID, message_id: uuid.UUID, channel: TenantChannel, meter: TurnMeter
    ) -> str:
        async with self._db.tenant_session(tenant_id) as s:
            msg = await s.get(Message, message_id)
            assert msg is not None
            media_url = msg.media_url or ""
            if (
                msg.msg_type != "audio"
                or msg.transcript
                or not media_url.startswith(MEDIA_PREFIXES)
            ):
                return msg.transcript or msg.body or ""
            media_id = media_url.split(":", 1)[1]
        if self._transcriber is None:
            return ""
        try:
            media = await self._sender(channel).download_media(media_id)
            tr = await self._transcriber.transcribe(media.content, media.mime_type)
        except (
            ChannelAPIError,
            ChannelNotConfiguredError,
            LLMUnavailableError,
            httpx.HTTPError,
        ) as exc:
            log.error("voice_note_failed", tenant_id=str(tenant_id), error=type(exc).__name__)
            return ""
        meter.add_raw(tr.model, tr.prompt_tokens, tr.completion_tokens, tr.latency_ms, tr.cost_usd)
        async with self._db.tenant_session(tenant_id) as s:
            msg = await s.get(Message, message_id)
            if msg is not None:
                msg.transcript = tr.text or None
        log.info(
            "voice_note_transcribed",
            tenant_id=str(tenant_id),
            chars=len(tr.text),
            latency_ms=tr.latency_ms,
        )
        return tr.text

    def _sender(self, channel: TenantChannel) -> ChannelSender:
        if self._sender_factory is not None:
            return self._sender_factory(channel)
        if channel.kind != "whatsapp":
            raise ChannelNotConfiguredError(f"no sender for a {channel.kind} channel")
        return self._client_factory(channel)

    async def _typing(self, channel: TenantChannel, conversation_id: uuid.UUID) -> None:
        """Best effort "typing…" on channels that show it (Telegram), before a model turn."""
        if channel.kind == "whatsapp":
            return
        try:
            sender = self._sender(channel)
            typing = getattr(sender, "typing", None)
            if typing is None:
                return
            async with self._db.tenant_session(channel.tenant_id) as s:
                conv = await s.get(Conversation, conversation_id)
                customer = await s.get(Customer, conv.customer_id) if conv else None
                to = await recipient_for(s, customer, channel.kind) if customer else None
            if to:
                await typing(to)
        except Exception as exc:  # noqa: BLE001 — cosmetic; never fail a turn over it
            log.info("typing_failed", error=type(exc).__name__)

    async def _channel(self, channel_id: uuid.UUID) -> TenantChannel:
        async with self._db.platform_session() as s:
            ch = await s.get(TenantChannel, channel_id)
        if ch is None:
            raise LookupError("channel not found")
        return ch

    async def _other_tenant_names(self, tenant_id: uuid.UUID, own_name: str) -> tuple[str, ...]:
        """Names of every OTHER tenant, for the validator's leak check. A name identical to this
        tenant's own is excluded — saying your own business name is not a leak."""
        async with self._db.platform_session() as s:
            names = (await s.scalars(select(Tenant.name).where(Tenant.id != tenant_id))).all()
        own = own_name.casefold()
        return tuple(sorted({n for n in names if n and len(n) >= 4 and n.casefold() != own}))

    async def _send_canned(
        self,
        persona: Persona,
        channel: TenantChannel,
        conversation_id: uuid.UUID,
        kind: Kind,
        language: str,
        with_disclosure: bool,
        meter: TurnMeter,
    ) -> None:
        text = canned(
            kind,
            language,
            agent_name=persona.agent_name,
            business_name=persona.business_name,
            with_disclosure=with_disclosure,
        )
        await self._send(channel, persona.tenant_id, conversation_id, text, meter, prompts.CANNED)

    async def _send(
        self,
        channel: TenantChannel,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        text: str,
        meter: TurnMeter,
        prompt_version: str,
    ) -> None:
        try:
            await send_text_reply(
                self._db,
                self._sender(channel),
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                text=text,
                llm=meter.usage(prompt_version),
            )
        except RecipientUnreachableError as exc:  # blocked us / no identity: retrying won't help
            log.warning(
                "reply_unreachable", tenant_id=str(tenant_id), channel=channel.kind,
                reason=str(exc),
            )  # fmt: skip
            await self._flush_meter(tenant_id, meter)
            return
        meter.recorded = True

    async def _flush_meter(self, tenant_id: uuid.UUID, meter: TurnMeter) -> None:
        """LLM spend with no outbound message (e.g. a suppressed turn) is still metered."""
        usage = meter.usage(None)
        if usage is None or meter.recorded:
            return
        async with self._db.tenant_session(tenant_id) as s:
            await record_usage(
                s,
                tenant_id,
                llm_prompt_tokens=usage.prompt_tokens,
                llm_completion_tokens=usage.completion_tokens,
                llm_cost_usd=usage.cost_usd,
            )
        meter.recorded = True

    async def _notify(self, tenant_id: uuid.UUID, esc: Escalation) -> None:
        if self._enqueuer is None:
            return
        try:
            await self._enqueuer.enqueue_job(
                JOB_NOTIFY,
                str(tenant_id),
                str(esc.conversation_id),
                esc.reason,
                esc.urgency,
                _job_id=f"notify:{esc.conversation_id}:{int(datetime.now(UTC).timestamp() // 60)}",
            )
        except (RedisError, OSError) as exc:
            log.error("escalation_notify_enqueue_failed", error=type(exc).__name__)

    async def _maybe_summarise(self, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        if self._enqueuer is None:
            return
        async with self._db.tenant_session(tenant_id) as s:
            inbound = await s.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id, Message.direction == "in")
            )
        n = self._settings.summary_every_n_inbound
        if inbound and inbound % n == 0:
            try:
                await self._enqueuer.enqueue_job(
                    JOB_SUMMARISE,
                    str(tenant_id),
                    str(conversation_id),
                    _job_id=f"sum:{conversation_id}:{inbound}",
                )
            except (RedisError, OSError) as exc:
                log.warning("summary_enqueue_failed", error=type(exc).__name__)

    async def _locked(
        self, conversation_id: uuid.UUID, fn: Callable[[], Any], job_try: int
    ) -> Outcome:
        key = f"lock:conv:{conversation_id}"
        token = uuid.uuid4().hex
        try:
            got = await self._redis.set(
                key, token, nx=True, px=self._settings.conversation_lock_ttl_s * 1000
            )
        except (RedisError, OSError) as exc:
            log.warning("conversation_lock_unavailable", error=type(exc).__name__)
            got = (
                True  # Redis down: proceed; the pending-inbound check still prevents double replies
            )
            token = ""
        if not got:
            return "locked"
        try:
            result: Outcome = await fn()
            return result
        finally:
            if token:
                try:
                    await self._redis.eval(_RELEASE_LUA, 1, key, token)  # type: ignore[misc]
                except (RedisError, OSError) as exc:
                    log.warning("conversation_lock_release_failed", error=type(exc).__name__)
