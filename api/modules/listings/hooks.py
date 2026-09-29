"""What the listings module contributes at runtime: the subject of a viewing (appointments'
`appointment_subject` extension point) and Today counts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Listing
from api.modules.base import Subject, SubjectError, TodayScope

PARAM = "listing_ref"


async def appointment_subject(s: AsyncSession, params: dict[str, Any]) -> Subject | None:
    """A viewing is about a listing: it must exist, be available and allow viewings. Its label
    and location are copied onto the appointment."""
    raw = params.get(PARAM)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or len(raw) > 64:
        raise SubjectError("invalid_listing_ref")
    ref = raw.strip().lstrip("#")
    x = await s.scalar(select(Listing).where(func.lower(Listing.ref) == ref.lower()))
    if x is None or x.status != "available":
        raise SubjectError("listing_not_available", listing_ref=ref)
    if not x.viewings_enabled:
        raise SubjectError("viewings_not_offered", listing_ref=x.ref)
    where = ", ".join(p for p in (x.address_note, x.community, x.area) if p)
    return Subject(
        module="listings", id=x.id, label=f"{x.ref} · {x.title}", location_note=where or None
    )


async def today(s: AsyncSession, _scope: TodayScope) -> dict[str, Any]:
    rows = (await s.execute(select(Listing.status, func.count()).group_by(Listing.status))).all()
    counts: dict[str, int] = {st: int(n) for st, n in rows}
    return {
        "available": int(counts.get("available", 0)),
        "under_offer": int(counts.get("under_offer", 0)),
        "draft": int(counts.get("draft", 0)),
    }
