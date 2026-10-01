"""/api/v1/platform — the HMH Labz console: every tenant's usage, margin and health, Meta
statement reconciliation, and the reimbursement ledger.

Cross-tenant reads never bypass RLS: the console opens one tenant session per tenant, exactly as
the tenant's own dashboard would. There is no database role that can see every tenant at once.

Margin (per calendar month, AED): revenue = monthly_fee_aed for the days after the free period
(free_months_until), cost = LLM spend (USD at the dirham peg) + Meta charges HMH Labz bears.
Shared infrastructure is not allocated per tenant.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

import httpx
from arq.constants import health_check_key_suffix
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy import func, select

from api.api.v1.common import amount, money
from api.api.v1.costs import LineOut, line_out
from api.billing.ledger import (
    LedgerError,
    LedgerMonth,
    borne_cost,
    mark_paid,
    month_usage,
    tenant_ledger,
)
from api.billing.periods import next_month, parse_month
from api.billing.statement import StatementError, pull, reconcile
from api.config import get_settings
from api.core.logging import get_logger
from api.db.models import (
    AuditLog,
    Conversation,
    Message,
    Tenant,
    TenantChannel,
    TenantModule,
    TenantSettings,
)
from api.meta.client import MetaAPIError, MetaClient
from api.meta.outbound import client_for_channel
from api.metering import PRICING_CATEGORIES
from api.platform.auth import OpsCtx, OwnerCtx, PlatformCtx, StaffCtx

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])
log = get_logger(__name__)

Health = Literal["ok", "degraded", "down"]
STUCK_AFTER = timedelta(minutes=5)
TOKEN_WARN = timedelta(days=7)


class ChannelOut(BaseModel):
    display_phone: str | None
    quality_rating: str | None
    messaging_limit_tier: str | None
    token_expires_at: datetime | None
    is_active: bool


class UsageOut(BaseModel):
    msgs_in: int
    msgs_out: int
    meta_cost_aed: str
    borne_aed: str
    llm_prompt_tokens: int
    llm_completion_tokens: int
    llm_cost_usd: str
    llm_cost_aed: str


class MarginOut(BaseModel):
    fee_aed: str | None
    revenue_aed: str | None
    direct_cost_aed: str
    margin_aed: str | None
    margin_pct: float | None


class TenantHealth(BaseModel):
    status: Health
    problems: list[str]
    last_inbound_at: datetime | None
    awaiting_human: int
    stuck: int
    failed_24h: int


class TenantRow(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    modules: list[str]
    channels: list[ChannelOut]
    usage: UsageOut
    cap_aed: str | None
    pct_of_cap: float | None
    margin: MarginOut
    health: TenantHealth
    statement: str  # the month's reconciliation status: ok | check | not_pulled | not_comparable
    free_until: date | None
    borne_until: date | None


class PlatformHealth(BaseModel):
    database: bool
    redis: bool
    worker: bool | None
    queue_depth: int | None


class OverviewOut(BaseModel):
    month: str
    platform: PlatformHealth
    tenants: list[TenantRow]
    totals: UsageOut
    revenue_aed: str
    direct_cost_aed: str


class DayOut(BaseModel):
    day: date
    msgs_in: int
    msgs_out: int
    counts: dict[str, int]
    meta_cost_aed: str
    llm_cost_usd: str


class StatementOut(BaseModel):
    pulled_at: datetime | None
    complete: bool
    currency: str | None
    meta_total_native: str | None
    total: LineOut | None
    categories: list[LineOut]
    days: list[LineOut]


class LedgerRow(BaseModel):
    tenant_id: uuid.UUID
    tenant_name: str
    service_month: int
    start: date
    end: date
    metered_aed: str
    statement_aed: str | None
    payable_aed: str
    basis: str
    state: str
    paid_aed: str | None
    paid_at: datetime | None
    reference: str | None


class TenantDetail(BaseModel):
    tenant: TenantRow
    days: list[DayOut]
    statement: StatementOut
    ledger: list[LedgerRow]


class LedgerOut(BaseModel):
    rows: list[LedgerRow]
    due_aed: str
    paid_aed: str


class PaidIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: uuid.UUID
    service_month: int = Field(ge=1, le=120)
    reference: str | None = Field(default=None, max_length=120)


# ---------------------------------------------------------------- helpers


def _month(value: str | None) -> date:
    try:
        return parse_month(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def revenue(fee: Decimal | None, month: date, free_until: date | None) -> Decimal | None:
    """The fee for the days of `month` after the free period."""
    if fee is None:
        return None
    days = calendar.monthrange(month.year, month.month)[1]
    if free_until is None or free_until < month:
        billable = days
    else:
        billable = max(0, (next_month(month) - free_until).days - 1)
    return fee * billable / days


@dataclass
class _Loaded:
    tenant: Tenant
    settings: TenantSettings | None
    modules: list[str]
    channels: list[TenantChannel]


async def _load(ctx: PlatformCtx, tenant_id: uuid.UUID | None = None) -> list[_Loaded]:
    async with ctx.db.platform_session() as s:
        q = select(Tenant).order_by(Tenant.name)
        if tenant_id is not None:
            q = q.where(Tenant.id == tenant_id)
        tenants = (await s.scalars(q)).all()
        ids = [t.id for t in tenants]
        settings = {
            x.tenant_id: x
            for x in (
                await s.scalars(select(TenantSettings).where(TenantSettings.tenant_id.in_(ids)))
            ).all()
        }
        modules: dict[uuid.UUID, list[str]] = {}
        for m in (
            await s.scalars(
                select(TenantModule)
                .where(TenantModule.tenant_id.in_(ids), TenantModule.enabled.is_(True))
                .order_by(TenantModule.module_key)
            )
        ).all():
            modules.setdefault(m.tenant_id, []).append(m.module_key)
        channels: dict[uuid.UUID, list[TenantChannel]] = {}
        for c in (
            await s.scalars(select(TenantChannel).where(TenantChannel.tenant_id.in_(ids)))
        ).all():
            channels.setdefault(c.tenant_id, []).append(c)
    return [
        _Loaded(t, settings.get(t.id), modules.get(t.id, []), channels.get(t.id, []))
        for t in tenants
    ]


async def _row(
    ctx: PlatformCtx, x: _Loaded, month: date, now: datetime
) -> tuple[TenantRow, list[DayOut], StatementOut]:
    peg = get_settings().usd_to_aed
    t = x.tenant
    async with ctx.db.tenant_session(t.id) as s:
        rows, totals = await month_usage(s, month)
        rec = await reconcile(s, month, usd_to_aed=peg)
        last_in = await s.scalar(
            select(func.max(Message.created_at)).where(Message.direction == "in")
        )
        awaiting = await s.scalar(
            select(func.count()).where(Conversation.state == "awaiting_human")
        )
        stuck = await s.scalar(
            select(func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.direction == "in",
                Message.status == "received",
                Message.created_at < now - STUCK_AFTER,
                Conversation.state != "awaiting_human",
            )
        )
        failed = await s.scalar(
            select(func.count()).where(
                Message.direction == "out",
                Message.status == "failed",
                Message.created_at >= now - timedelta(hours=24),
            )
        )

    borne = borne_cost(rows, t.meta_charges_borne_by_us_until)
    llm_aed = totals.llm_cost_usd * peg
    direct = llm_aed + borne
    fee = t.monthly_fee_aed
    rev = revenue(fee, month, t.free_months_until)
    margin = rev - direct if rev is not None else None
    cap = x.settings.monthly_message_cap_aed if x.settings else None

    problems: list[str] = []
    down = False
    active = [c for c in x.channels if c.is_active]
    if not active:
        problems.append("no active WhatsApp number")
        down = True
    for c in active:
        if c.token_expires_at is not None and c.token_expires_at < now:
            problems.append("access token expired")
            down = True
        elif c.token_expires_at is not None and c.token_expires_at < now + TOKEN_WARN:
            problems.append("access token expires within 7 days")
        if c.quality_rating in ("YELLOW", "RED"):
            problems.append(f"quality rating {c.quality_rating}")
    if stuck:
        problems.append(f"{stuck} message(s) unanswered for over 5 minutes")
    if failed:
        problems.append(f"{failed} failed send(s) in 24 h")
    if cap and totals.meta_cost_aed >= cap * Decimal("0.8"):
        problems.append("at 80% or more of the message cap")
    health: Health = "down" if down else ("degraded" if problems else "ok")

    usage = UsageOut(
        msgs_in=totals.msgs_in,
        msgs_out=totals.msgs_out,
        meta_cost_aed=amount(totals.meta_cost_aed),
        borne_aed=amount(borne),
        llm_prompt_tokens=totals.llm_prompt_tokens,
        llm_completion_tokens=totals.llm_completion_tokens,
        llm_cost_usd=str(totals.llm_cost_usd.quantize(Decimal("0.0001"))),
        llm_cost_aed=amount(llm_aed),
    )
    row = TenantRow(
        id=t.id,
        name=t.name,
        slug=t.slug,
        status=t.status,
        modules=x.modules,
        channels=[
            ChannelOut(
                display_phone=c.display_phone,
                quality_rating=c.quality_rating,
                messaging_limit_tier=c.messaging_limit_tier,
                token_expires_at=c.token_expires_at,
                is_active=c.is_active,
            )
            for c in x.channels
        ],
        usage=usage,
        cap_aed=money(cap),
        pct_of_cap=round(float(totals.meta_cost_aed / cap * 100), 1) if cap else None,
        margin=MarginOut(
            fee_aed=money(fee),
            revenue_aed=money(rev),
            direct_cost_aed=amount(direct),
            margin_aed=money(margin),
            margin_pct=round(float(margin / rev * 100), 1) if margin is not None and rev else None,
        ),
        health=TenantHealth(
            status=health,
            problems=problems,
            last_inbound_at=last_in,
            awaiting_human=int(awaiting or 0),
            stuck=int(stuck or 0),
            failed_24h=int(failed or 0),
        ),
        statement=rec.total.status if rec.total else "not_pulled",
        free_until=t.free_months_until,
        borne_until=t.meta_charges_borne_by_us_until,
    )
    days = [
        DayOut(
            day=r.day,
            msgs_in=r.msgs_in,
            msgs_out=r.msgs_out,
            counts={c: getattr(r, f"{c}_count") for c in PRICING_CATEGORIES},
            meta_cost_aed=amount(r.meta_cost_aed),
            llm_cost_usd=str(r.llm_cost_usd.quantize(Decimal("0.0001"))),
        )
        for r in rows
    ]
    statement = StatementOut(
        pulled_at=rec.pulled_at,
        complete=rec.complete,
        currency=rec.currency,
        meta_total_native=money(rec.meta_total_native),
        total=line_out(rec.total) if rec.total else None,
        categories=[line_out(line) for line in rec.categories],
        days=[line_out(line) for line in rec.days],
    )
    return row, days, statement


def _ledger_row(m: LedgerMonth) -> LedgerRow:
    return LedgerRow(
        tenant_id=m.tenant_id,
        tenant_name=m.tenant_name,
        service_month=m.number,
        start=m.start,
        end=m.end,
        metered_aed=amount(m.metered_aed),
        statement_aed=money(m.statement_aed),
        payable_aed=amount(m.payable_aed),
        basis=m.basis,
        state=m.state,
        paid_aed=money(m.paid.amount_aed) if m.paid else None,
        paid_at=m.paid.paid_at if m.paid else None,
        reference=m.paid.reference if m.paid else None,
    )


async def _platform_health(ctx: PlatformCtx) -> PlatformHealth:
    settings = get_settings()
    try:
        db_ok = await ctx.db.ping()
    except Exception:  # noqa: BLE001 — health reporting: any failure means "down"
        db_ok = False
    try:
        redis_ok = bool(await ctx.redis.ping())
        worker = bool(await ctx.redis.exists(settings.arq_queue_name + health_check_key_suffix))
        depth = int(await ctx.redis.zcard(settings.arq_queue_name))
    except (RedisError, OSError):
        redis_ok, worker, depth = False, None, None
    return PlatformHealth(database=db_ok, redis=redis_ok, worker=worker, queue_depth=depth)


# ---------------------------------------------------------------- routes


@router.get("/overview", response_model=OverviewOut)
async def overview(ctx: StaffCtx, month: str | None = None) -> OverviewOut:
    first = _month(month)
    now = datetime.now(UTC)
    rows = [(await _row(ctx, x, first, now))[0] for x in await _load(ctx)]
    total = UsageOut(
        msgs_in=sum(r.usage.msgs_in for r in rows),
        msgs_out=sum(r.usage.msgs_out for r in rows),
        meta_cost_aed=amount(sum((Decimal(r.usage.meta_cost_aed) for r in rows), Decimal(0))),
        borne_aed=amount(sum((Decimal(r.usage.borne_aed) for r in rows), Decimal(0))),
        llm_prompt_tokens=sum(r.usage.llm_prompt_tokens for r in rows),
        llm_completion_tokens=sum(r.usage.llm_completion_tokens for r in rows),
        llm_cost_usd=str(sum((Decimal(r.usage.llm_cost_usd) for r in rows), Decimal(0))),
        llm_cost_aed=amount(sum((Decimal(r.usage.llm_cost_aed) for r in rows), Decimal(0))),
    )
    return OverviewOut(
        month=first.strftime("%Y-%m"),
        platform=await _platform_health(ctx),
        tenants=rows,
        totals=total,
        revenue_aed=amount(
            sum((Decimal(r.margin.revenue_aed) for r in rows if r.margin.revenue_aed), Decimal(0))
        ),
        direct_cost_aed=amount(sum((Decimal(r.margin.direct_cost_aed) for r in rows), Decimal(0))),
    )


async def _detail(ctx: PlatformCtx, tenant_id: uuid.UUID, first: date) -> TenantDetail:
    loaded = await _load(ctx, tenant_id)
    if not loaded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    row, days, statement = await _row(ctx, loaded[0], first, datetime.now(UTC))
    ledger = await tenant_ledger(
        ctx.db,
        loaded[0].tenant,
        today=datetime.now(UTC).date(),
        usd_to_aed=get_settings().usd_to_aed,
    )
    return TenantDetail(
        tenant=row, days=days, statement=statement, ledger=[_ledger_row(m) for m in ledger]
    )


@router.get("/tenants/{tenant_id}", response_model=TenantDetail)
async def tenant_detail(
    ctx: StaffCtx, tenant_id: uuid.UUID, month: str | None = None
) -> TenantDetail:
    return await _detail(ctx, tenant_id, _month(month))


@router.post("/tenants/{tenant_id}/statement/pull", response_model=TenantDetail)
async def pull_statement(
    ctx: OpsCtx, request: Request, tenant_id: uuid.UUID, month: str | None = None
) -> TenantDetail:
    first = _month(month)
    settings = get_settings()
    http: httpx.AsyncClient = request.app.state.http

    def client_for(ch: TenantChannel) -> MetaClient:
        return client_for_channel(ch, http, settings)

    try:
        result = await pull(ctx.db, tenant_id, first, client_for)
    except StatementError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MetaAPIError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Meta: {exc}") from exc
    async with ctx.db.tenant_session(tenant_id) as s:
        s.add(
            AuditLog(
                actor=ctx.staff.actor,
                action="pull_meta_statement",
                entity="meta_statement",
                after={
                    "month": first.isoformat(),
                    "lines": result.lines,
                    "complete": result.complete,
                },
            )
        )
    return await _detail(ctx, tenant_id, first)


@router.get("/reimbursements", response_model=LedgerOut)
async def reimbursements(ctx: StaffCtx) -> LedgerOut:
    today = datetime.now(UTC).date()
    peg = get_settings().usd_to_aed
    rows: list[LedgerRow] = []
    due = paid = Decimal(0)
    for x in await _load(ctx):
        for m in await tenant_ledger(ctx.db, x.tenant, today=today, usd_to_aed=peg):
            rows.append(_ledger_row(m))
            if m.state == "due":
                due += m.payable_aed
            elif m.paid is not None:
                paid += m.paid.amount_aed
    return LedgerOut(rows=rows, due_aed=amount(due), paid_aed=amount(paid))


@router.post("/reimbursements", response_model=LedgerRow, status_code=201)
async def record_reimbursement(ctx: OwnerCtx, body: PaidIn) -> LedgerRow:
    loaded = await _load(ctx, body.tenant_id)
    if not loaded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    try:
        m = await mark_paid(
            ctx.db,
            loaded[0].tenant,
            body.service_month,
            reference=body.reference,
            actor=ctx.staff.actor,
            today=datetime.now(UTC).date(),
            usd_to_aed=get_settings().usd_to_aed,
        )
    except LedgerError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    async with ctx.db.tenant_session(body.tenant_id) as s:
        s.add(
            AuditLog(
                actor=ctx.staff.actor,
                action="record_reimbursement",
                entity="reimbursement",
                after={
                    "service_month": m.number,
                    "amount_aed": amount(m.payable_aed),
                    "basis": m.basis,
                },
            )
        )
    log.info("reimbursement_recorded", tenant_id=str(body.tenant_id), service_month=m.number)
    return _ledger_row(m)
