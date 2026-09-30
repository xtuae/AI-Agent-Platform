"""/api/v1/m/campaigns — templates, segments and campaigns (Phase 4).

Reading is open to every role. Everything that could lead to a message being sent — templates,
campaigns, approving, starting — is admin-only. Nothing here sends a message itself: starting a
campaign queues the sender, which applies every guard of 02 §4.4 again at send time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from api.agents.context import load_persona
from api.api.v1.common import (
    MAX_PAGE,
    In,
    conflict,
    load_tenant,
    local_today,
    money,
    not_found,
    unprocessable,
    zone,
)
from api.auth.deps import Admin, Ctx, Viewer
from api.config import get_settings
from api.core.logging import get_logger
from api.db.models import (
    Campaign,
    CampaignRecipient,
    Customer,
    MessageTemplate,
    TenantChannel,
    TenantSettings,
)
from api.llm.router import LLMRequestError, LLMRouter, LLMUnavailableError
from api.meta.client import MetaAPIError, MetaClient
from api.meta.outbound import ChannelNotConfiguredError, client_for_channel
from api.metering import record_usage
from api.modules.base import SegmentScope
from api.modules.campaigns import guards, segments, service
from api.modules.campaigns.bindings import SOURCES, Binding
from api.modules.campaigns.config import config_of
from api.modules.campaigns.sender import JOB
from api.modules.campaigns.templates import (
    NAME,
    Check,
    DraftError,
    Variable,
    check,
    draft,
    placeholder_count,
)
from api.modules.registry import Enabled

log = get_logger(__name__)
router = APIRouter()

Category = Literal["MARKETING", "UTILITY"]
Language = Literal["en", "ar"]


# ---------------------------------------------------------------- schemas


class CheckOut(BaseModel):
    errors: list[str]
    warnings: list[str]


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    language: str
    category: str | None
    body: str | None
    variables: list[Variable]
    meta_status: str | None
    rejected_reason: str | None
    source: str
    submitted_at: datetime | None
    created_at: datetime
    check: CheckOut


class TemplateIn(In):
    name: str = Field(pattern=r"^[a-z0-9_]{1,100}$")
    language: Language
    category: Category = "MARKETING"
    body: str = Field(min_length=1, max_length=1024)
    variables: list[Variable] = Field(default_factory=list, max_length=20)


class TemplatePatch(In):
    body: str | None = Field(default=None, min_length=1, max_length=1024)
    category: Category | None = None
    variables: list[Variable] | None = Field(default=None, max_length=20)


class CheckIn(In):
    body: str = Field(max_length=2000)
    variables: list[Variable] = Field(default_factory=list, max_length=20)


class BriefIn(In):
    objective: str = Field(min_length=3, max_length=300)
    audience: str = Field(default="", max_length=300)
    offer: str = Field(default="", max_length=300)
    products: str = Field(default="", max_length=300)
    language: Language = "en"


class DraftOut(BaseModel):
    name: str
    category: Category
    language: Language
    body: str
    variables: list[Variable]
    rationale: str
    check: CheckOut


class SyncOut(BaseModel):
    updated: int
    imported: int


class SegmentIn(In):
    definition: dict[str, Any]


class CountOut(BaseModel):
    recipients: int
    definition: dict[str, Any]


class CampaignIn(In):
    name: str = Field(min_length=1, max_length=120)
    template_id: uuid.UUID | None = None
    segment: dict[str, Any] | None = None
    variable_bindings: list[Binding] = Field(default_factory=list, max_length=20)
    budget_cap_aed: Decimal | None = Field(
        default=None, gt=0, le=1_000_000, max_digits=10, decimal_places=2
    )
    throttle_per_minute: int | None = Field(default=None, ge=1, le=600)


class CampaignPatch(In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    template_id: uuid.UUID | None = None
    segment: dict[str, Any] | None = None
    variable_bindings: list[Binding] | None = Field(default=None, max_length=20)
    budget_cap_aed: Decimal | None = Field(
        default=None, gt=0, le=1_000_000, max_digits=10, decimal_places=2
    )
    throttle_per_minute: int | None = Field(default=None, ge=1, le=600)


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    template_id: uuid.UUID | None
    template_name: str | None
    segment: dict[str, Any] | None
    variable_bindings: list[dict[str, Any]]
    budget_cap_aed: str | None
    throttle_per_minute: int
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    paused_reason: str | None
    created_at: datetime
    recipient_count: int
    sent_count: int
    delivered_count: int
    read_count: int
    reply_count: int
    skipped_count: int
    failed_count: int
    spend_aed: str


class Results(BaseModel):
    by_status: dict[str, int]
    skip_reasons: dict[str, int]
    modules: dict[str, dict[str, Any]]  # e.g. orders: attributed orders and value


class CampaignDetail(CampaignOut):
    results: Results


class PreviewOut(BaseModel):
    recipients: int
    estimated_cost_aed: str | None
    sample: str | None
    sample_to: str | None
    problems: list[str]  # what stops approval right now


class RecipientOut(BaseModel):
    customer_id: uuid.UUID
    name: str | None
    wa_id: str
    status: str | None
    skip_reason: str | None
    sent_at: datetime | None
    replied_at: datetime | None
    cost_aed: str | None


class RecipientPage(BaseModel):
    items: list[RecipientOut]
    total: int


class Guardrails(BaseModel):
    quality_rating: str | None
    messaging_limit_tier: str | None
    quality_block: str | None
    may_send_now: bool
    next_send_time: datetime | None
    frequency_days: int
    sent_last_24h: int
    tier_limit: int | None


# ---------------------------------------------------------------- helpers


def _check_out(c: Check) -> CheckOut:
    return CheckOut(errors=c.errors, warnings=c.warnings)


def _variables(t: MessageTemplate) -> list[Variable]:
    out: list[Variable] = []
    for v in t.variables or []:
        try:
            out.append(Variable.model_validate(v))
        except ValueError:
            continue
    return out


async def _links_allowed(ctx: Ctx) -> bool:
    return config_of((await ctx.modules()).config("campaigns")).links_allowed


def _template_out(t: MessageTemplate, links_allowed: bool) -> TemplateOut:
    variables = _variables(t)
    return TemplateOut(
        id=t.id,
        name=t.name,
        language=t.language,
        category=t.category,
        body=t.body,
        variables=variables,
        meta_status=t.meta_status,
        rejected_reason=t.rejected_reason,
        source=t.source,
        submitted_at=t.submitted_at,
        created_at=t.created_at,
        check=_check_out(check(t.body or "", variables, links_allowed=links_allowed)),
    )


def _campaign_out(c: Campaign, template_name: str | None) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        status=c.status,
        template_id=c.template_id,
        template_name=template_name,
        segment=c.segment_query,
        variable_bindings=list(c.variable_bindings or []),
        budget_cap_aed=money(c.budget_cap_aed),
        throttle_per_minute=c.throttle_per_minute,
        approved_by=c.approved_by,
        approved_at=c.approved_at,
        started_at=c.started_at,
        finished_at=c.finished_at,
        paused_reason=c.paused_reason,
        created_at=c.created_at,
        recipient_count=c.recipient_count,
        sent_count=c.sent_count,
        delivered_count=c.delivered_count,
        read_count=c.read_count,
        reply_count=c.reply_count,
        skipped_count=c.skipped_count,
        failed_count=c.failed_count,
        spend_aed=money(c.spend_aed) or "0.00",
    )


def _error(exc: service.CampaignError | segments.SegmentError) -> HTTPException:
    return unprocessable(exc.code, **exc.details)


async def _scope(ctx: Ctx) -> SegmentScope:
    tenant = await load_tenant(ctx)
    now = datetime.now(UTC)
    return SegmentScope(now=now, today=local_today(tenant, now))


async def _channel(ctx: Ctx) -> TenantChannel:
    async with ctx.platform() as s:
        channel = await s.scalar(
            select(TenantChannel)
            .where(TenantChannel.tenant_id == ctx.tenant_id, TenantChannel.is_active.is_(True))
            .order_by(TenantChannel.id)
            .limit(1)
        )
    if channel is None or channel.waba_id is None:
        raise conflict({"code": "no_whatsapp_account"})
    return channel


def _client(request: Request, channel: TenantChannel) -> MetaClient:
    http: httpx.AsyncClient = request.app.state.http
    try:
        return client_for_channel(channel, http, get_settings())
    except ChannelNotConfiguredError as exc:
        raise conflict({"code": "no_whatsapp_account"}) from exc


def _meta_failed(exc: MetaAPIError) -> HTTPException:
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY, {"code": "whatsapp_rejected", "meta_code": exc.code}
    )


def _clean_segment(definition: dict[str, Any] | None, enabled: Enabled) -> dict[str, Any] | None:
    if definition is None:
        return None
    try:
        return segments.validate(definition, enabled)
    except segments.SegmentError as exc:
        raise _error(exc) from exc


async def _load(ctx: Ctx, s: Any, campaign_id: uuid.UUID, *, lock: bool = False) -> Campaign:
    c: Campaign | None = await s.get(Campaign, campaign_id, with_for_update=lock)
    if c is None:
        raise not_found("campaign")
    return c


async def _template_name(s: Any, c: Campaign) -> str | None:
    if c.template_id is None:
        return None
    t = await s.get(MessageTemplate, c.template_id)
    return t.name if t else None


async def _enqueue(request: Request, ctx: Ctx, campaign_id: uuid.UUID) -> None:
    await request.app.state.arq.enqueue_job(
        JOB,
        str(ctx.tenant_id),
        str(campaign_id),
        _job_id=f"campaign:{campaign_id}:start:{uuid.uuid4().hex[:8]}",
    )


# ---------------------------------------------------------------- guardrails


@router.get("/guardrails", response_model=Guardrails)
async def guardrails(ctx: Viewer) -> Guardrails:
    tenant = await load_tenant(ctx)
    cfg = config_of((await ctx.modules()).config("campaigns"))
    async with ctx.platform() as s:
        channel = await s.scalar(
            select(TenantChannel)
            .where(TenantChannel.tenant_id == ctx.tenant_id, TenantChannel.is_active.is_(True))
            .limit(1)
        )
        ts = await s.get(TenantSettings, ctx.tenant_id)
    now = datetime.now(UTC)
    tz = zone(tenant)
    hours = ts.business_hours if ts else None
    async with ctx.tx() as s:
        used = await guards.sent_last_24h(s, now)
    return Guardrails(
        quality_rating=channel.quality_rating if channel else None,
        messaging_limit_tier=channel.messaging_limit_tier if channel else None,
        quality_block=guards.quality_block(channel) if channel else None,
        may_send_now=guards.may_send_at(now, hours, tz),
        next_send_time=guards.next_send_time(now, hours, tz),
        frequency_days=cfg.frequency_days,
        sent_last_24h=used,
        tier_limit=guards.tier_limit(channel) if channel else None,
    )


# ---------------------------------------------------------------- templates


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(ctx: Viewer) -> list[TemplateOut]:
    links = await _links_allowed(ctx)
    async with ctx.tx() as s:
        rows = (
            await s.scalars(select(MessageTemplate).order_by(MessageTemplate.created_at.desc()))
        ).all()
    return [_template_out(t, links) for t in rows]


@router.post("/templates/check", response_model=CheckOut)
async def check_template(body: CheckIn, ctx: Viewer) -> CheckOut:
    return _check_out(check(body.body, body.variables, links_allowed=await _links_allowed(ctx)))


@router.post("/templates/draft", response_model=DraftOut)
async def draft_template(body: BriefIn, ctx: Admin, request: Request) -> DraftOut:
    """The copy drafter (02 §4.1): a suggestion for a person to edit. Nothing is saved."""
    persona = await load_persona(ctx.db, ctx.tenant_id)
    llm: LLMRouter = request.app.state.llm
    try:
        result, usage = await draft(
            llm,
            provider=persona.llm_provider,
            model=persona.chat_model,
            brief={
                "business_name": persona.business_name,
                "business_description": persona.business_description,
                "service_areas": persona.service_areas,
                "objective": body.objective,
                "segment_description": body.audience,
                "offer_details": body.offer,
                "products": body.products,
                "language": "Arabic" if body.language == "ar" else "English",
            },
        )
    except (LLMUnavailableError, LLMRequestError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, {"code": "drafter_unavailable"}
        ) from exc
    except DraftError as exc:
        raise unprocessable("draft_unusable") from exc
    async with ctx.tx() as s:  # the drafter's tokens are metered like any other LLM call
        await record_usage(
            s,
            ctx.tenant_id,
            llm_prompt_tokens=usage.prompt_tokens,
            llm_completion_tokens=usage.completion_tokens,
            llm_cost_usd=usage.cost_usd,
        )
    return DraftOut(
        **result.model_dump(),
        check=_check_out(
            check(result.body, result.variables, links_allowed=await _links_allowed(ctx))
        ),
    )


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateIn, ctx: Admin) -> TemplateOut:
    links = await _links_allowed(ctx)
    result = check(body.body, body.variables, links_allowed=links)
    if not result.ok:
        raise unprocessable("template_rules", errors=result.errors)
    async with ctx.tx() as s:
        exists = await s.scalar(
            select(MessageTemplate.id).where(
                MessageTemplate.name == body.name, MessageTemplate.language == body.language
            )
        )
        if exists is not None:
            raise conflict({"code": "template_exists"})
        t = MessageTemplate(
            name=body.name,
            language=body.language,
            category=body.category,
            body=body.body.strip(),
            variables=[v.model_dump() for v in body.variables],
            created_by=ctx.principal.actor,
            source="drafted",
        )
        s.add(t)
        await s.flush()
        await s.refresh(t)
        return _template_out(t, links)


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def update_template(template_id: uuid.UUID, body: TemplatePatch, ctx: Admin) -> TemplateOut:
    links = await _links_allowed(ctx)
    async with ctx.tx() as s:
        t = await s.get(MessageTemplate, template_id, with_for_update=True)
        if t is None:
            raise not_found("template")
        if t.meta_status not in (None, "REJECTED"):
            raise conflict({"code": "template_submitted", "status": t.meta_status})
        new_body = body.body.strip() if body.body is not None else t.body or ""
        variables = body.variables if body.variables is not None else _variables(t)
        result = check(new_body, variables, links_allowed=links)
        if not result.ok:
            raise unprocessable("template_rules", errors=result.errors)
        t.body = new_body
        t.variables = [v.model_dump() for v in variables]
        if body.category is not None:
            t.category = body.category
        if t.meta_status == "REJECTED":  # edited after a rejection: ready to submit again
            t.meta_status, t.rejected_reason = None, None
        await s.flush()
        await s.refresh(t)
        return _template_out(t, links)


@router.post("/templates/{template_id}/submit", response_model=TemplateOut)
async def submit_template(template_id: uuid.UUID, ctx: Admin, request: Request) -> TemplateOut:
    links = await _links_allowed(ctx)
    async with ctx.tx() as s:
        t = await s.get(MessageTemplate, template_id)
        if t is None:
            raise not_found("template")
        if t.meta_status not in (None, "REJECTED"):
            raise conflict({"code": "template_submitted", "status": t.meta_status})
        variables = _variables(t)
        result = check(t.body or "", variables, links_allowed=links)
        if not result.ok:
            raise unprocessable("template_rules", errors=result.errors)
        name, language, category, text = t.name, t.language, t.category or "MARKETING", t.body or ""
    channel = await _channel(ctx)
    assert channel.waba_id is not None
    examples = [v.example for v in sorted(variables, key=lambda v: v.index)]
    try:
        meta_id, meta_status = await _client(request, channel).create_template(
            channel.waba_id,
            name=name,
            language=language,
            category=category,
            body=text,
            examples=examples,
        )
    except MetaAPIError as exc:
        raise _meta_failed(exc) from exc
    async with ctx.tx() as s:
        t2 = await s.get(MessageTemplate, template_id, with_for_update=True)
        assert t2 is not None
        t2.meta_template_id = meta_id
        t2.meta_status = (meta_status or "PENDING").upper()
        t2.submitted_at = datetime.now(UTC)
        t2.rejected_reason = None
        await s.flush()
        await s.refresh(t2)
        return _template_out(t2, links)


@router.post("/templates/sync", response_model=SyncOut)
async def sync_templates(ctx: Admin, request: Request) -> SyncOut:
    """Re-read every template on the WhatsApp account: statuses of ours, and templates made in
    Meta's own tools (imported so a campaign can use them)."""
    channel = await _channel(ctx)
    assert channel.waba_id is not None
    try:
        infos = await _client(request, channel).list_templates(channel.waba_id)
    except MetaAPIError as exc:
        raise _meta_failed(exc) from exc
    updated = imported = 0
    async with ctx.tx() as s:
        for info in infos:
            language = info.language or "en"
            t = await s.scalar(
                select(MessageTemplate).where(
                    MessageTemplate.name == info.name, MessageTemplate.language == language
                )
            )
            body = info.body_text()
            if t is None:
                if body is None or not NAME.fullmatch(info.name):
                    continue
                n = placeholder_count(body)
                s.add(
                    MessageTemplate(
                        name=info.name,
                        language=language,
                        category=(info.category or "").upper() or None,
                        body=body,
                        variables=[
                            {"index": i, "meaning": "", "example": f"value {i}"}
                            for i in range(1, n + 1)
                        ],
                        meta_template_id=info.id,
                        meta_status=info.status.upper(),
                        rejected_reason=info.rejected_reason,
                        source="meta",
                        created_by=ctx.principal.actor,
                    )
                )
                imported += 1
            elif (t.meta_status or "") != info.status.upper() or t.meta_template_id != info.id:
                t.meta_status = info.status.upper()
                t.meta_template_id = info.id
                t.rejected_reason = info.rejected_reason
                updated += 1
    return SyncOut(updated=updated, imported=imported)


# ---------------------------------------------------------------- segments


@router.get("/segment-fields")
async def segment_fields(ctx: Viewer) -> dict[str, Any]:
    return {
        "fields": segments.describe(await ctx.modules()),
        "variable_sources": [{"source": k, "label": v} for k, v in SOURCES.items()],
    }


@router.post("/segments/count", response_model=CountOut)
async def count_segment(body: SegmentIn, ctx: Viewer) -> CountOut:
    enabled = await ctx.modules()
    scope = await _scope(ctx)
    try:
        clean = segments.validate(body.definition, enabled)
        async with ctx.tx() as s:
            n = int(await s.scalar(segments.count(clean, enabled, scope)) or 0)
    except segments.SegmentError as exc:
        raise _error(exc) from exc
    return CountOut(recipients=n, definition=clean)


# ---------------------------------------------------------------- campaigns


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(ctx: Viewer) -> list[CampaignOut]:
    async with ctx.tx() as s:
        rows = (
            await s.execute(
                select(Campaign, MessageTemplate.name)
                .outerjoin(MessageTemplate, MessageTemplate.id == Campaign.template_id)
                .order_by(Campaign.created_at.desc())
                .limit(MAX_PAGE)
            )
        ).all()
    return [_campaign_out(c, name) for c, name in rows]


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(body: CampaignIn, ctx: Admin) -> CampaignOut:
    enabled = await ctx.modules()
    cfg = config_of(enabled.config("campaigns"))
    segment = _clean_segment(body.segment, enabled)
    async with ctx.tx() as s:
        if body.template_id is not None and await s.get(MessageTemplate, body.template_id) is None:
            raise not_found("template")
        c = Campaign(
            name=body.name.strip(),
            template_id=body.template_id,
            segment_query=segment,
            variable_bindings=[b.model_dump() for b in body.variable_bindings],
            budget_cap_aed=body.budget_cap_aed,
            throttle_per_minute=body.throttle_per_minute or cfg.throttle_per_minute,
            created_by=ctx.principal.actor,
        )
        s.add(c)
        await s.flush()
        await s.refresh(c)
        service.audit(s, ctx.principal.actor, "create_campaign", c)
        return _campaign_out(c, await _template_name(s, c))


@router.get("/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(campaign_id: uuid.UUID, ctx: Viewer) -> CampaignDetail:
    enabled = await ctx.modules()
    cfg = config_of(enabled.config("campaigns"))
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id)
        by_status = {
            str(st): int(n)
            for st, n in (
                await s.execute(
                    select(CampaignRecipient.status, func.count())
                    .where(CampaignRecipient.campaign_id == campaign_id)
                    .group_by(CampaignRecipient.status)
                )
            ).all()
        }
        skips = {
            str(r): int(n)
            for r, n in (
                await s.execute(
                    select(CampaignRecipient.skip_reason, func.count())
                    .where(
                        CampaignRecipient.campaign_id == campaign_id,
                        CampaignRecipient.skip_reason.is_not(None),
                    )
                    .group_by(CampaignRecipient.skip_reason)
                )
            ).all()
        }
        figures: dict[str, dict[str, Any]] = {}
        for m in enabled.modules:
            if m.campaign_attribution is not None:
                figures[m.key] = await m.campaign_attribution(s, campaign_id, cfg.attribution_days)
        base = _campaign_out(c, await _template_name(s, c))
    return CampaignDetail(
        **base.model_dump(),
        results=Results(by_status=by_status, skip_reasons=skips, modules=figures),
    )


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def update_campaign(campaign_id: uuid.UUID, body: CampaignPatch, ctx: Admin) -> CampaignOut:
    enabled = await ctx.modules()
    changes = body.model_dump(exclude_unset=True)
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id, lock=True)
        if c.status != "draft":
            raise conflict({"code": "not_a_draft", "status": c.status})
        new_template = changes.get("template_id")
        if new_template is not None and await s.get(MessageTemplate, new_template) is None:
            raise not_found("template")
        if "segment" in changes:
            c.segment_query = _clean_segment(changes.pop("segment"), enabled)
        if "variable_bindings" in changes:
            c.variable_bindings = [b.model_dump() for b in body.variable_bindings or []]
            changes.pop("variable_bindings")
        for k, v in changes.items():
            if k == "throttle_per_minute" and v is None:
                continue
            setattr(c, k, v.strip() if isinstance(v, str) else v)
        await s.flush()
        await s.refresh(c)
        return _campaign_out(c, await _template_name(s, c))


@router.get("/{campaign_id}/preview", response_model=PreviewOut)
async def preview_campaign(campaign_id: uuid.UUID, ctx: Viewer) -> PreviewOut:
    enabled = await ctx.modules()
    scope = await _scope(ctx)
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id)
        problems: list[str] = []
        try:
            service.check_ready(c, await service.template_for(s, c))
        except service.CampaignError as exc:
            problems.append(exc.code)
        try:
            p = await service.preview(s, c, enabled, scope)
        except segments.SegmentError as exc:
            raise _error(exc) from exc
    if p.recipients == 0:
        problems.append("no_recipients")
    return PreviewOut(
        recipients=p.recipients,
        estimated_cost_aed=money(p.estimated_cost_aed),
        sample=p.sample,
        sample_to=p.sample_to,
        problems=problems,
    )


@router.post("/{campaign_id}/approve", response_model=CampaignOut)
async def approve_campaign(campaign_id: uuid.UUID, ctx: Admin) -> CampaignOut:
    """The human approval gate (constraint 8): records who approved it and when."""
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id, lock=True)
        try:
            service.check_ready(c, await service.template_for(s, c))
            service.approve(s, c, ctx.principal.user_id, ctx.principal.actor, datetime.now(UTC))
        except service.CampaignError as exc:
            raise _error(exc) from exc
        await s.flush()
        return _campaign_out(c, await _template_name(s, c))


@router.post("/{campaign_id}/start", response_model=CampaignOut)
async def start_campaign(campaign_id: uuid.UUID, ctx: Admin, request: Request) -> CampaignOut:
    enabled = await ctx.modules()
    scope = await _scope(ctx)
    async with ctx.platform() as p:
        channel = await p.scalar(
            select(TenantChannel).where(
                TenantChannel.tenant_id == ctx.tenant_id, TenantChannel.is_active.is_(True)
            )
        )
    block = guards.quality_block(channel) if channel else None
    if block:
        raise conflict({"code": "quality_block", "reason": block})
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id, lock=True)
        try:
            await service.start(s, c, enabled, scope, ctx.principal.actor)
        except service.CampaignError as exc:
            raise _error(exc) from exc
        except segments.SegmentError as exc:
            raise _error(exc) from exc
        await s.flush()
        out = _campaign_out(c, await _template_name(s, c))
    await _enqueue(request, ctx, campaign_id)
    return out


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(campaign_id: uuid.UUID, ctx: Admin) -> CampaignOut:
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id, lock=True)
        try:
            service.pause(s, c, "paused_by_team", ctx.principal.actor)
        except service.CampaignError as exc:
            raise _error(exc) from exc
        await s.flush()
        return _campaign_out(c, await _template_name(s, c))


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume_campaign(campaign_id: uuid.UUID, ctx: Admin, request: Request) -> CampaignOut:
    async with ctx.platform() as p:
        channel = await p.scalar(
            select(TenantChannel).where(
                TenantChannel.tenant_id == ctx.tenant_id, TenantChannel.is_active.is_(True)
            )
        )
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id, lock=True)
        try:
            service.resume(
                s, c, ctx.principal.actor, guards.quality_block(channel) if channel else None
            )
        except service.CampaignError as exc:
            raise _error(exc) from exc
        await s.flush()
        out = _campaign_out(c, await _template_name(s, c))
    await _enqueue(request, ctx, campaign_id)
    return out


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(campaign_id: uuid.UUID, ctx: Admin) -> CampaignOut:
    async with ctx.tx() as s:
        c = await _load(ctx, s, campaign_id, lock=True)
        try:
            service.cancel(s, c, ctx.principal.actor)
        except service.CampaignError as exc:
            raise _error(exc) from exc
        await s.flush()
        return _campaign_out(c, await _template_name(s, c))


@router.get("/{campaign_id}/recipients", response_model=RecipientPage)
async def recipients(
    campaign_id: uuid.UUID,
    ctx: Viewer,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecipientPage:
    conds: list[Any] = [CampaignRecipient.campaign_id == campaign_id]
    if status_:
        conds.append(CampaignRecipient.status == status_)
    async with ctx.tx() as s:
        await _load(ctx, s, campaign_id)
        total = int(
            await s.scalar(select(func.count()).select_from(CampaignRecipient).where(*conds)) or 0
        )
        rows = (
            await s.execute(
                select(CampaignRecipient, Customer)
                .join(Customer, Customer.id == CampaignRecipient.customer_id)
                .where(*conds)
                .order_by(
                    CampaignRecipient.sent_at.desc().nulls_last(), CampaignRecipient.created_at
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return RecipientPage(
        items=[
            RecipientOut(
                customer_id=c.id,
                name=c.name,
                wa_id=c.wa_id,
                status=r.status,
                skip_reason=r.skip_reason,
                sent_at=r.sent_at,
                replied_at=r.replied_at,
                cost_aed=money(r.cost_aed),
            )
            for r, c in rows
        ],
        total=total,
    )
