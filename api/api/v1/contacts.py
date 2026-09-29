"""/api/v1/contacts — the people a business talks to (the `customers` table). Core, for every
tenant: search, profile, opt-in status and its evidence, conversations. Each enabled module adds a
panel under `modules.<key>` (orders: history; coupons: books and balance).

Opt-in can only be recorded from the customer's own action (the agent's record_opt_in, or an
import with evidence in Phase 5) — the dashboard cannot mark anyone opted in, because TDRA needs
proof we would not have. Opting someone OUT from the dashboard is always allowed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Select, func, or_, select

from api.api.v1.common import (
    MAX_PAGE,
    In,
    audit,
    conflict,
    load_tenant,
    local_today,
    normalise_wa_id,
    not_found,
    unprocessable,
)
from api.auth.deps import Agent, Viewer
from api.db.models import Conversation, Customer

router = APIRouter(prefix="/contacts", tags=["contacts"])

OptIn = Literal["pending", "opted_in", "opted_out"]
EDITABLE = ("name", "area", "emirate", "address_note", "language")


class CustomerRow(BaseModel):
    id: uuid.UUID
    wa_id: str
    name: str | None
    area: str | None
    emirate: str | None
    language: str | None
    source: str | None
    opt_in_status: OptIn


class ConversationBrief(BaseModel):
    id: uuid.UUID
    state: str
    last_inbound_at: datetime | None


class CustomerDetail(CustomerRow):
    address_note: str | None
    external_ref: str | None
    opt_in_at: datetime | None
    opt_in_evidence: dict[str, Any] | None
    opt_out_at: datetime | None
    conversations: list[ConversationBrief]
    modules: dict[str, dict[str, Any]]  # one panel per enabled module that has one


class CustomerPage(BaseModel):
    items: list[CustomerRow]
    total: int


class CustomerCreate(In):
    wa_id: str = Field(min_length=6, max_length=24)
    name: str | None = Field(default=None, max_length=120)
    area: str | None = Field(default=None, max_length=120)
    emirate: str | None = Field(default=None, max_length=60)
    address_note: str | None = Field(default=None, max_length=300)
    language: Literal["en", "ar", "ar-latn"] | None = None

    @field_validator("wa_id")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalise_wa_id(v)


class CustomerPatch(In):
    name: str | None = Field(default=None, max_length=120)
    area: str | None = Field(default=None, max_length=120)
    emirate: str | None = Field(default=None, max_length=60)
    address_note: str | None = Field(default=None, max_length=300)
    language: Literal["en", "ar", "ar-latn"] | None = None
    opt_out: Literal[True] | None = None  # the only opt-in change a person can make here


def _row(c: Customer) -> CustomerRow:
    return CustomerRow(
        id=c.id,
        wa_id=c.wa_id,
        name=c.name,
        area=c.area,
        emirate=c.emirate,
        language=c.language,
        source=c.source,
        opt_in_status=c.opt_in_status,  # type: ignore[arg-type]  # DB CHECK
    )


def _search(stmt: Select[Any], q: str | None, opt_in: OptIn | None, area: str | None) -> Any:
    if q:
        like = f"%{q.lower()}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        conds = [func.lower(Customer.name).like(like), func.lower(Customer.area).like(like)]
        if len(digits) >= 3:
            conds.append(Customer.wa_id.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    if opt_in:
        stmt = stmt.where(Customer.opt_in_status == opt_in)
    if area:
        stmt = stmt.where(func.lower(Customer.area) == area.lower())
    return stmt


@router.get("", response_model=CustomerPage)
async def list_customers(
    ctx: Viewer,
    q: Annotated[str | None, Query(max_length=80)] = None,
    opt_in: OptIn | None = None,
    area: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CustomerPage:
    q = q.strip() if q else None
    async with ctx.tx() as s:
        total = int(
            await s.scalar(_search(select(func.count()).select_from(Customer), q, opt_in, area))
            or 0
        )
        rows = (
            await s.scalars(
                _search(select(Customer), q, opt_in, area)
                .order_by(Customer.last_order_at.desc().nulls_last(), Customer.name)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return CustomerPage(items=[_row(c) for c in rows], total=total)


async def _detail(ctx: Viewer | Agent, customer_id: uuid.UUID) -> CustomerDetail:
    today = local_today(await load_tenant(ctx))
    enabled = await ctx.modules()
    panels: dict[str, dict[str, Any]] = {}
    async with ctx.tx() as s:
        c = await s.get(Customer, customer_id)
        if c is None:
            raise not_found("customer")
        convs = (
            await s.scalars(
                select(Conversation)
                .where(Conversation.customer_id == c.id)
                .order_by(Conversation.last_inbound_at.desc().nulls_last())
                .limit(20)
            )
        ).all()
        for module in enabled.modules:
            if module.contact_panel is not None:
                panels[module.key] = await module.contact_panel(s, c.id, today)
    return CustomerDetail(
        **_row(c).model_dump(),
        address_note=c.address_note,
        external_ref=c.external_ref,
        opt_in_at=c.opt_in_at,
        opt_in_evidence=c.opt_in_evidence,
        opt_out_at=c.opt_out_at,
        conversations=[
            ConversationBrief(id=v.id, state=v.state, last_inbound_at=v.last_inbound_at)
            for v in convs
        ],
        modules=panels,
    )


@router.get("/{customer_id}", response_model=CustomerDetail)
async def get_customer(customer_id: uuid.UUID, ctx: Viewer) -> CustomerDetail:
    return await _detail(ctx, customer_id)


@router.post("", response_model=CustomerDetail, status_code=status.HTTP_201_CREATED)
async def create_customer(body: CustomerCreate, ctx: Agent) -> CustomerDetail:
    """For an order taken by phone from someone the agent has never spoken to."""
    async with ctx.tx() as s:
        existing = await s.scalar(select(Customer.id).where(Customer.wa_id == body.wa_id))
        if existing is not None:
            raise conflict({"code": "customer_exists", "id": str(existing)})
        c = Customer(**body.model_dump(), source="dashboard", opt_in_status="pending")
        s.add(c)
        await s.flush()
        audit(s, ctx, "create_customer", "customer", c.id, after={"source": "dashboard"})
        new_id = c.id
    return await _detail(ctx, new_id)


@router.patch("/{customer_id}", response_model=CustomerDetail)
async def update_customer(
    customer_id: uuid.UUID, body: CustomerPatch, ctx: Agent
) -> CustomerDetail:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise unprocessable("nothing_to_change")
    async with ctx.tx() as s:
        c = await s.get(Customer, customer_id, with_for_update=True)
        if c is None:
            raise not_found("customer")
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        if changes.pop("opt_out", None) and c.opt_in_status != "opted_out":
            before["opt_in_status"], after["opt_in_status"] = c.opt_in_status, "opted_out"
            c.opt_in_status = "opted_out"
            c.opt_out_at = datetime.now(UTC)
        for name in EDITABLE:
            if name in changes and getattr(c, name) != changes[name]:
                # PII fields are audited by name only; the values stay in the customers table.
                before[name] = "…"
                after[name] = "changed"
                setattr(c, name, changes[name])
        if after:
            audit(s, ctx, "update_customer", "customer", c.id, before=before, after=after)
    return await _detail(ctx, customer_id)
