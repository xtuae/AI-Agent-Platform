"""/api/v1/optin — the tenant's consent pages (QR codes / short URLs) and how they perform.

optin_links is a platform table (the public page resolves a code before any tenant context), so
every query here filters on ctx.tenant_id explicitly. Changing the wording affects future visits
only: each visit keeps the wording that was on screen when it happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

import segno
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from api.api.v1.common import In, audit, load_tenant, not_found, unprocessable
from api.auth.deps import Admin, Ctx, Viewer
from api.config import get_settings
from api.db.models import OptinLink, OptinVisit
from api.optin.service import new_code, telegram_bot, whatsapp_number

router = APIRouter(prefix="/optin", tags=["optin"])

Lang = Literal["en", "ar"]
Source = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")]

DEFAULTS: dict[str, dict[str, str]] = {
    "en": {
        "heading": "Get offers from {business} on WhatsApp",
        "wording": "Yes, I'd like to receive offers, reminders and updates from {business} on "
        "WhatsApp. I can stop them at any time by replying STOP.",
        "prefill": "Yes, please send me offers from {business}",
    },
    "ar": {
        "heading": "احصل على عروض {business} عبر واتساب",
        "wording": "نعم، أرغب في تلقي العروض والتذكيرات والتحديثات من {business} عبر واتساب. "
        "ويمكنني إيقافها في أي وقت بإرسال STOP.",
        "prefill": "نعم، أرسلوا لي عروض {business}",
    },
}


class LinkIn(In):
    label: str = Field(min_length=1, max_length=80)
    source: Source = "qr_code"
    language: Lang = "en"
    heading: str = Field(min_length=3, max_length=120)
    wording: str = Field(min_length=20, max_length=1000)
    prefill: str = Field(min_length=2, max_length=160)


class LinkPatch(In):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    source: Source | None = None
    heading: str | None = Field(default=None, min_length=3, max_length=120)
    wording: str | None = Field(default=None, min_length=20, max_length=1000)
    prefill: str | None = Field(default=None, min_length=2, max_length=160)
    is_active: bool | None = None


class LinkOut(BaseModel):
    id: uuid.UUID
    code: str
    url: str
    label: str
    source: str
    language: Lang
    heading: str
    wording: str
    prefill: str
    is_active: bool
    created_at: datetime
    visits: int
    opted_in: int


class LinksOut(BaseModel):
    links: list[LinkOut]
    whatsapp_ready: bool  # no active number → the pages cannot hand over to WhatsApp
    telegram_ready: bool = False  # a live bot → the pages also offer "Continue on Telegram"


class Defaults(BaseModel):
    heading: str
    wording: str
    prefill: str


class QrOut(BaseModel):
    url: str
    svg: str


def _base(request: Request) -> str:
    configured = get_settings().optin_base_url
    return (configured or str(request.base_url)).rstrip("/")


def _out(link: OptinLink, base: str, counts: dict[uuid.UUID, tuple[int, int]]) -> LinkOut:
    visits, claimed = counts.get(link.id, (0, 0))
    return LinkOut(
        id=link.id,
        code=link.code,
        url=f"{base}/q/{link.code}",
        label=link.label,
        source=link.source,
        language=link.language,  # type: ignore[arg-type]
        heading=link.heading,
        wording=link.wording,
        prefill=link.prefill,
        is_active=link.is_active,
        created_at=link.created_at,
        visits=visits,
        opted_in=claimed,
    )


async def _counts(ctx: Ctx) -> dict[uuid.UUID, tuple[int, int]]:
    async with ctx.tx() as s:
        rows = (
            await s.execute(
                select(
                    OptinVisit.link_id,
                    func.count(),
                    func.count(OptinVisit.claimed_at),
                ).group_by(OptinVisit.link_id)
            )
        ).all()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


async def _link(ctx: Ctx, link_id: uuid.UUID) -> OptinLink:
    async with ctx.platform() as s:
        link = await s.scalar(
            select(OptinLink).where(OptinLink.id == link_id, OptinLink.tenant_id == ctx.tenant_id)
        )
    if link is None:
        raise not_found("link")
    return link


@router.get("/links", response_model=LinksOut)
async def list_links(ctx: Viewer, request: Request) -> LinksOut:
    async with ctx.platform() as s:
        links = (
            await s.scalars(
                select(OptinLink)
                .where(OptinLink.tenant_id == ctx.tenant_id)
                .order_by(OptinLink.is_active.desc(), OptinLink.created_at.desc())
            )
        ).all()
        ready = await whatsapp_number(s, ctx.tenant_id) is not None
        tg_ready = await telegram_bot(s, ctx.tenant_id) is not None
    counts = await _counts(ctx)
    base = _base(request)
    return LinksOut(
        links=[_out(link, base, counts) for link in links],
        whatsapp_ready=ready,
        telegram_ready=tg_ready,
    )


@router.get("/defaults", response_model=Defaults)
async def defaults(ctx: Viewer, language: Lang = "en") -> Defaults:
    tenant = await load_tenant(ctx)
    d = DEFAULTS[language]
    return Defaults(**{k: v.format(business=tenant.name) for k, v in d.items()})


@router.post("/links", response_model=LinkOut, status_code=201)
async def create_link(ctx: Admin, body: LinkIn, request: Request) -> LinkOut:
    for _ in range(5):
        try:
            async with ctx.platform() as s:
                link = OptinLink(
                    tenant_id=ctx.tenant_id,
                    code=new_code(),
                    created_by=ctx.principal.actor,
                    **body.model_dump(),
                )
                s.add(link)
                await s.flush()
                await s.refresh(link)
            break
        except IntegrityError:  # code collision: 31^8 space, but never assume
            continue
    else:  # pragma: no cover
        raise unprocessable("code_unavailable")
    async with ctx.tx() as s:
        audit(s, ctx, "create_optin_link", "optin_link", link.id, after=body.model_dump())
    return _out(link, _base(request), {})


@router.patch("/links/{link_id}", response_model=LinkOut)
async def update_link(ctx: Admin, link_id: uuid.UUID, body: LinkPatch, request: Request) -> LinkOut:
    changes = body.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise unprocessable("null_not_allowed")
    await _link(ctx, link_id)
    async with ctx.platform() as s:
        link = await s.scalar(
            select(OptinLink)
            .where(OptinLink.id == link_id, OptinLink.tenant_id == ctx.tenant_id)
            .with_for_update()
        )
        assert link is not None
        before = {k: getattr(link, k) for k in changes}
        for k, v in changes.items():
            setattr(link, k, v)
        link.updated_at = func.now()
        await s.flush()
        await s.refresh(link)
    async with ctx.tx() as s:
        audit(s, ctx, "update_optin_link", "optin_link", link.id, before=before, after=changes)
    return _out(link, _base(request), await _counts(ctx))


@router.get("/links/{link_id}/qr", response_model=QrOut)
async def qr(ctx: Viewer, link_id: uuid.UUID, request: Request) -> QrOut:
    link = await _link(ctx, link_id)
    url = f"{_base(request)}/q/{link.code}"
    svg = segno.make(url, error="m").svg_inline(scale=8, border=2, dark="#000", light="#fff")
    return QrOut(url=url, svg=svg)
