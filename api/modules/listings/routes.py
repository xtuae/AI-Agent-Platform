"""/api/v1/m/listings — the properties the agent can talk about. Reading is open to every role;
creating and editing needs `agent` (the people who take listings on). Nothing is deleted: a
listing leaves the market by status (let, sold) or is archived."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select

from api.api.v1.common import MAX_PAGE, In, audit, conflict, money, not_found
from api.auth.deps import Agent, Viewer
from api.db.models import Listing
from api.modules.listings.models import ListingStatus, PropertyType, Purpose, RentPeriod

router = APIRouter()


class ListingOut(BaseModel):
    id: uuid.UUID
    ref: str
    title: str
    description: str | None
    purpose: Purpose
    property_type: PropertyType
    area: str | None
    community: str | None
    address_note: str | None
    bedrooms: int | None
    bathrooms: int | None
    size_sqft: int | None
    price_aed: str | None
    rent_period: RentPeriod | None
    status: ListingStatus
    viewings_enabled: bool
    created_at: datetime
    updated_at: datetime


class ListingPage(BaseModel):
    items: list[ListingOut]
    total: int


Money = Annotated[Decimal, Field(ge=0, le=10_000_000_000, max_digits=14, decimal_places=2)]


class _Fields(In):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    purpose: Purpose | None = None
    property_type: PropertyType | None = None
    area: str | None = Field(default=None, max_length=120)
    community: str | None = Field(default=None, max_length=120)
    address_note: str | None = Field(default=None, max_length=300)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    bathrooms: int | None = Field(default=None, ge=0, le=20)
    size_sqft: int | None = Field(default=None, ge=0, le=10_000_000)
    price_aed: Money | None = None
    rent_period: RentPeriod | None = None
    status: ListingStatus | None = None
    viewings_enabled: bool | None = None


class ListingCreate(_Fields):
    ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._/-]+$")
    title: str = Field(min_length=1, max_length=160)
    purpose: Purpose
    property_type: PropertyType
    status: ListingStatus = "draft"
    viewings_enabled: bool = True

    @model_validator(mode="after")
    def _rules(self) -> ListingCreate:
        _check(self.status, self.price_aed)
        return self


class ListingPatch(_Fields):
    pass


def _check(status_: str | None, price: Decimal | None) -> None:
    if status_ == "available" and price is None:
        raise ValueError("an available listing needs a price")


def _out(x: Listing) -> ListingOut:
    return ListingOut(
        id=x.id,
        ref=x.ref,
        title=x.title,
        description=x.description,
        purpose=x.purpose,  # type: ignore[arg-type]  # DB CHECK guarantees the literal
        property_type=x.property_type,  # type: ignore[arg-type]
        area=x.area,
        community=x.community,
        address_note=x.address_note,
        bedrooms=x.bedrooms,
        bathrooms=x.bathrooms,
        size_sqft=x.size_sqft,
        price_aed=money(x.price_aed),
        rent_period=x.rent_period,  # type: ignore[arg-type]
        status=x.status,  # type: ignore[arg-type]
        viewings_enabled=x.viewings_enabled,
        created_at=x.created_at,
        updated_at=x.updated_at,
    )


def _jsonable(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


@router.get("", response_model=ListingPage)
async def list_listings(
    ctx: Viewer,
    q: Annotated[str | None, Query(max_length=80)] = None,
    status_: Annotated[list[ListingStatus] | None, Query(alias="status")] = None,
    purpose: Purpose | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListingPage:
    conds: list[Any] = []
    if status_:
        conds.append(Listing.status.in_(status_))
    else:
        conds.append(Listing.status != "archived")
    if purpose:
        conds.append(Listing.purpose == purpose)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        conds.append(
            or_(
                func.lower(Listing.ref).like(like),
                func.lower(Listing.title).like(like),
                func.lower(Listing.area).like(like),
                func.lower(Listing.community).like(like),
            )
        )
    async with ctx.tx() as s:
        total = int(await s.scalar(select(func.count()).select_from(Listing).where(*conds)) or 0)
        rows = (
            await s.scalars(
                select(Listing)
                .where(*conds)
                .order_by(Listing.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return ListingPage(items=[_out(x) for x in rows], total=total)


@router.get("/{listing_id}", response_model=ListingOut)
async def get_listing(listing_id: uuid.UUID, ctx: Viewer) -> ListingOut:
    async with ctx.tx() as s:
        x = await s.get(Listing, listing_id)
        if x is None:
            raise not_found("listing")
        return _out(x)


@router.post("", response_model=ListingOut, status_code=status.HTTP_201_CREATED)
async def create_listing(body: ListingCreate, ctx: Agent) -> ListingOut:
    async with ctx.tx() as s:
        if await s.scalar(select(Listing.id).where(func.lower(Listing.ref) == body.ref.lower())):
            raise conflict({"code": "ref_exists"})
        values = {k: v for k, v in body.model_dump().items() if v is not None}
        x = Listing(**values)
        s.add(x)
        await s.flush()
        await s.refresh(x)
        audit(
            s,
            ctx,
            "create_listing",
            "listing",
            x.id,
            after={k: _jsonable(v) for k, v in values.items()},
        )
        return _out(x)


@router.patch("/{listing_id}", response_model=ListingOut)
async def update_listing(listing_id: uuid.UUID, body: ListingPatch, ctx: Agent) -> ListingOut:
    changes = body.model_dump(exclude_unset=True)
    for required in ("title", "purpose", "property_type", "status", "viewings_enabled"):
        if required in changes and changes[required] is None:
            raise conflict({"code": "required", "field": required})
    async with ctx.tx() as s:
        x = await s.get(Listing, listing_id, with_for_update=True)
        if x is None:
            raise not_found("listing")
        try:
            _check(changes.get("status", x.status), changes.get("price_aed", x.price_aed))
        except ValueError as exc:
            raise conflict({"code": "available_needs_price"}) from exc
        before = {k: _jsonable(getattr(x, k)) for k in changes}
        for k, v in changes.items():
            setattr(x, k, v)
        if changes:
            audit(
                s,
                ctx,
                "update_listing",
                "listing",
                x.id,
                before=before,
                after={k: _jsonable(v) for k, v in changes.items()},
            )
        await s.flush()
        await s.refresh(x)
        return _out(x)
