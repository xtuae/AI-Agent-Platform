"""/api/v1/today — the Today screen: live conversations, awaiting-human count,
month-to-date message spend against the cap, agent health, and the data for its two charts.

Spend comes from usage_daily (UTC days, as metered at send time). For a tenant whose Meta charges
HMH Labz bears (tenants.meta_charges_borne_by_us_until), `spend.borne_by_hmh` is true and the
dashboard shows "Borne by HMH Labz" instead of an amount due; the figure is still returned so the
cost is visible.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from arq.constants import health_check_key_suffix
from fastapi import APIRouter
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import func, select

from api.api.v1.common import load_tenant, local_today, money
from api.auth.deps import Viewer
from api.config import get_settings
from api.db.models import Conversation, Message, TenantChannel, TenantSettings, UsageDaily
from api.modules.base import TodayScope

router = APIRouter(tags=["today"])

STUCK_AFTER = timedelta(minutes=5)
TOKEN_WARN = timedelta(days=7)

Health = Literal["ok", "degraded", "down"]


class Spend(BaseModel):
    month: str  # YYYY-MM
    meta_cost_aed: str
    cap_aed: str | None
    pct_of_cap: float | None
    messages_out: int
    borne_by_hmh: bool
    borne_until: date | None


class HealthCheck(BaseModel):
    name: str
    status: Health
    detail: str


class AgentHealth(BaseModel):
    status: Health
    last_agent_reply_at: datetime | None
    checks: list[HealthCheck]


class SpendPoint(BaseModel):
    day: date
    cumulative_aed: str


class TodayOut(BaseModel):
    date: date
    live_conversations: int
    awaiting_human: int
    spend: Spend
    health: AgentHealth
    # each enabled module's Today data (orders: count, value, deliveries due, 14-day chart)
    modules: dict[str, dict[str, Any]]
    spend_by_day: list[SpendPoint]


def _worst(statuses: list[Health]) -> Health:
    for s in ("down", "degraded"):
        if s in statuses:
            return s
    return "ok"


@router.get("/today", response_model=TodayOut)
async def today(ctx: Viewer) -> TodayOut:
    settings = get_settings()
    tenant = await load_tenant(ctx)
    now = datetime.now(UTC)
    day = local_today(tenant, now)
    month_start = now.date().replace(day=1)  # usage_daily days are UTC

    async with ctx.platform() as s:
        tset = await s.get(TenantSettings, ctx.tenant_id)
        channels = (
            await s.scalars(
                select(TenantChannel).where(
                    TenantChannel.tenant_id == ctx.tenant_id, TenantChannel.is_active.is_(True)
                )
            )
        ).all()

    enabled = await ctx.modules()
    scope = TodayScope(tenant_id=ctx.tenant_id, timezone=tenant.timezone, today=day)
    module_data: dict[str, dict[str, Any]] = {}
    async with ctx.tx() as s:
        for module in enabled.modules:
            if module.today is not None:
                module_data[module.key] = await module.today(s, scope)
        live = await s.scalar(
            select(func.count()).where(
                Conversation.state != "closed", Conversation.service_window_expires_at > now
            )
        )
        awaiting = await s.scalar(
            select(func.count()).where(Conversation.state == "awaiting_human")
        )
        usage = (
            await s.execute(
                select(UsageDaily.day, UsageDaily.meta_cost_aed, UsageDaily.msgs_out)
                .where(UsageDaily.day >= month_start)
                .order_by(UsageDaily.day)
            )
        ).all()
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
        last_reply = await s.scalar(
            select(func.max(Message.created_at)).where(
                Message.direction == "out", Message.llm_model.is_not(None)
            )
        )

    # --- spend
    cost = sum((u.meta_cost_aed for u in usage), Decimal(0))
    cap = tset.monthly_message_cap_aed if tset else None
    borne_until = tenant.meta_charges_borne_by_us_until
    running = Decimal(0)
    spend_points: list[SpendPoint] = []
    for u in usage:
        running += u.meta_cost_aed
        spend_points.append(SpendPoint(day=u.day, cumulative_aed=money(running) or "0.00"))

    # --- health
    worker_up: bool | None
    try:
        worker_up = bool(await ctx.redis.exists(settings.arq_queue_name + health_check_key_suffix))
    except (RedisError, OSError):
        worker_up = None
    checks = [
        HealthCheck(
            name="worker",
            status="ok" if worker_up else "down",
            detail="processing messages"
            if worker_up
            else ("cannot reach the queue" if worker_up is None else "not running"),
        ),
        HealthCheck(
            name="backlog",
            status="degraded" if stuck else "ok",
            detail=f"{stuck} customer message(s) unanswered for over 5 minutes"
            if stuck
            else "no unanswered messages",
        ),
        HealthCheck(
            name="delivery",
            status="degraded" if failed else "ok",
            detail=f"{failed} message(s) failed to deliver in 24 h"
            if failed
            else "no failed messages in 24 h",
        ),
    ]
    if not channels:
        checks.append(
            HealthCheck(name="whatsapp", status="down", detail="no active WhatsApp number")
        )
    else:
        soon = [
            c
            for c in channels
            if c.token_expires_at is not None and c.token_expires_at < now + TOKEN_WARN
        ]
        checks.append(
            HealthCheck(
                name="whatsapp",
                status="degraded" if soon else "ok",
                detail="access token expires within 7 days" if soon else "connected",
            )
        )

    pct: float | None = None
    if cap:
        with contextlib.suppress(ArithmeticError):
            pct = round(float(cost / cap * 100), 1)

    return TodayOut(
        date=day,
        live_conversations=int(live or 0),
        awaiting_human=int(awaiting or 0),
        spend=Spend(
            month=month_start.strftime("%Y-%m"),
            meta_cost_aed=money(cost) or "0.00",
            cap_aed=money(cap),
            pct_of_cap=pct,
            messages_out=sum(u.msgs_out for u in usage),
            borne_by_hmh=borne_until is not None and day <= borne_until,
            borne_until=borne_until,
        ),
        health=AgentHealth(
            status=_worst([c.status for c in checks]),
            last_agent_reply_at=last_reply,
            checks=checks,
        ),
        modules=module_data,
        spend_by_day=spend_points,
    )
