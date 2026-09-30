"""Appointments and listings modules (04_modules_appointments_listings.md): availability, the
no-double-booking guard under a race, the agent tools and their guards, the listing subject, the
dashboard routes, presets and prompts.

The clock is fixed: NOW is Monday 2030-01-07 10:00 in Dubai (06:00 UTC), and every resource works
09:00-12:00 Dubai time, every day, unless a test says otherwise.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.agents.prompts import compose
from api.agents.text import estimate_tokens
from api.agents.tools import execute, toolset
from api.agents.tools.base import ToolContext
from api.config import Settings
from api.db.models import (
    Appointment,
    AppointmentResource,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
    Conversation,
    Customer,
    Listing,
)
from api.db.session import Database
from api.modules import admin as modules_admin
from api.modules import registry
from api.modules.appointments import service
from api.modules.appointments.config import AppointmentsConfig
from api.modules.registry import Enabled, enabled_of
from api.tests.conftest import DashHarness, make_channel, make_customer, make_tenant

DUBAI = ZoneInfo("Asia/Dubai")
NOW = datetime(2030, 1, 7, 6, 0, tzinfo=UTC)  # Monday 10:00 Dubai
MON = date(2030, 1, 7)
TUE = MON + timedelta(days=1)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), DUBAI).astimezone(UTC)


def local_times(slots: list[service.Slot]) -> list[str]:
    return [s.starts_at.astimezone(DUBAI).strftime("%a %H:%M") for s in slots]


@dataclass
class Shop:
    tenant: uuid.UUID
    customer: uuid.UUID
    other_customer: uuid.UUID
    conversation: uuid.UUID
    consult: uuid.UUID  # 60 min, office
    viewing: uuid.UUID  # 30 min, on site, needs an address
    r1: uuid.UUID
    keys: tuple[str, ...]


async def make_shop(
    db: Database, *, keys: tuple[str, ...] = ("appointments",), resources: int = 1
) -> Shop:
    tenant = await make_tenant(db, "appt", modules=keys)
    customer = await make_customer(db, tenant, "971500000401")
    other = await make_customer(db, tenant, "971500000402")
    channel = await make_channel(db, tenant)
    async with db.tenant_session(tenant) as s:
        conv = Conversation(customer_id=customer, channel_id=channel.id)
        consult = AppointmentType(
            name_en="Consultation", name_ar="استشارة", duration_min=60, location_kind="office"
        )
        viewing = AppointmentType(
            name_en="Viewing", duration_min=30, location_kind="onsite", needs_address=True
        )
        rs = [AppointmentResource(name=f"Agent {i + 1}") for i in range(resources)]
        s.add_all([conv, consult, viewing, *rs])
        await s.flush()
        for r in rs:
            s.add_all(
                AvailabilityRule(resource_id=r.id, weekday=d, start_time=time(9), end_time=time(12))
                for d in range(7)
            )
        return Shop(tenant, customer, other, conv.id, consult.id, viewing.id, rs[0].id, keys)


def enabled(keys: tuple[str, ...], **cfg: Any) -> Enabled:
    base = enabled_of(keys)
    configs = dict(base.configs)
    configs["appointments"] = AppointmentsConfig(**cfg)
    return Enabled(modules=base.modules, configs=configs)


@asynccontextmanager
async def tool_ctx(
    db: Database, shop: Shop, *, customer: uuid.UUID | None = None, now: datetime = NOW, **cfg: Any
) -> AsyncIterator[ToolContext]:
    async with db.tenant_session(shop.tenant) as s:
        yield ToolContext(
            session=s,
            tenant_id=shop.tenant,
            customer_id=customer or shop.customer,
            conversation_id=shop.conversation,
            inbound_wamid=None,
            now=now,
            today=now.astimezone(DUBAI).date(),
            timezone="Asia/Dubai",
            enabled=enabled(shop.keys, **cfg),
        )


async def call(db: Database, shop: Shop, tool: str, args: dict[str, Any], **kw: Any) -> Any:
    async with tool_ctx(db, shop, **kw) as ctx:
        return await execute(ctx, tool, json.dumps(args))


async def slots(
    db: Database, shop: Shop, *, type_id: uuid.UUID | None = None, days: int = 2, **cfg: Any
) -> list[service.Slot]:
    async with db.tenant_session(shop.tenant) as s:
        t = await s.get(AppointmentType, type_id or shop.consult)
        assert t is not None
        return await service.free_slots(
            s,
            type_=t,
            day_from=MON,
            day_to=MON + timedelta(days=days - 1),
            tz=DUBAI,
            now=NOW,
            config=AppointmentsConfig(**cfg),
        )


async def insert(
    db: Database, shop: Shop, start: datetime, minutes: int = 60, **kw: Any
) -> uuid.UUID:
    async with db.tenant_session(shop.tenant) as s:
        a = Appointment(
            ref=f"T-{uuid.uuid4().hex[:6]}",
            customer_id=shop.other_customer,
            type_id=shop.consult,
            resource_id=kw.pop("resource_id", shop.r1),
            starts_at=start,
            ends_at=start + timedelta(minutes=minutes),
            status=kw.pop("status", "confirmed"),
            source="dashboard",
            **kw,
        )
        s.add(a)
        await s.flush()
        return a.id


# ---------------------------------------------------------------- availability


async def test_slots_respect_hours_notice_and_duration(db: Database) -> None:
    shop = await make_shop(db)
    # Monday: 11:00 would end at 12:00 but starts inside the 2 h notice; nothing left.
    # Tuesday: every half hour whose hour fits before 12:00.
    assert local_times(await slots(db, shop)) == [
        "Tue 09:00",
        "Tue 09:30",
        "Tue 10:00",
        "Tue 10:30",
        "Tue 11:00",
    ]
    assert local_times(await slots(db, shop, min_notice_hours=0)) == [
        "Mon 10:00",
        "Mon 10:30",
        "Mon 11:00",
        "Tue 09:00",
        "Tue 09:30",
        "Tue 10:00",
        "Tue 10:30",
        "Tue 11:00",
    ]
    assert local_times(await slots(db, shop, slot_minutes=60)) == [
        "Tue 09:00",
        "Tue 10:00",
        "Tue 11:00",
    ]
    # horizon: nothing beyond today + horizon_days
    assert await slots(db, shop, horizon_days=1, days=5) == await slots(db, shop, days=2)


async def test_time_off_and_existing_bookings_and_buffer(db: Database) -> None:
    shop = await make_shop(db)
    async with db.tenant_session(shop.tenant) as s:
        s.add(
            AvailabilityException(
                resource_id=shop.r1, day=TUE, start_time=time(10), end_time=time(11)
            )
        )
    assert local_times(await slots(db, shop)) == ["Tue 09:00", "Tue 11:00"]
    async with db.tenant_session(shop.tenant) as s:
        s.add(AvailabilityException(resource_id=None, day=TUE, reason="holiday"))
    assert await slots(db, shop) == []  # "everyone" whole-day off


async def test_booked_time_and_buffer_are_not_offered(db: Database) -> None:
    shop = await make_shop(db)
    await insert(db, shop, at(TUE, 10))
    assert local_times(await slots(db, shop)) == ["Tue 09:00", "Tue 11:00"]
    assert await slots(db, shop, buffer_minutes=30) == []
    # a cancelled booking frees its time
    shop2 = await make_shop(db)
    await insert(db, shop2, at(TUE, 10), status="cancelled")
    assert len(await slots(db, shop2)) == 5


async def test_a_slot_is_free_if_any_resource_is(db: Database) -> None:
    shop = await make_shop(db, resources=2)
    await insert(db, shop, at(TUE, 10))
    found = {s.starts_at: s.resource_ids for s in await slots(db, shop)}
    assert len(found) == 5
    assert shop.r1 not in found[at(TUE, 10)]
    assert len(found[at(TUE, 9)]) == 2


def test_spread_offers_a_choice_of_days() -> None:
    all_slots = [
        service.Slot(at(MON + timedelta(days=d), h), at(MON + timedelta(days=d), h + 1), ())
        for d in range(4)
        for h in (9, 10, 11, 12, 13)
    ]
    picked = service.spread(all_slots, DUBAI)
    assert len(picked) == service.MAX_OFFERED
    per_day: dict[date, int] = {}
    for sl in picked:
        per_day[sl.starts_at.astimezone(DUBAI).date()] = (
            per_day.get(sl.starts_at.astimezone(DUBAI).date(), 0) + 1
        )
    assert max(per_day.values()) == service.PER_DAY


# ---------------------------------------------------------------- the overlap guard (database)


async def test_database_refuses_overlapping_live_appointments(db: Database) -> None:
    shop = await make_shop(db)
    await insert(db, shop, at(TUE, 10))
    with pytest.raises(IntegrityError, match="appointment_overlap"):
        await insert(db, shop, at(TUE, 10, 30))
    await insert(db, shop, at(TUE, 11))  # back to back is fine
    await insert(db, shop, at(TUE, 10, 30), status="cancelled")  # a dead one never blocks
    moving = await insert(db, shop, at(TUE, 9), minutes=30)
    async with db.tenant_session(shop.tenant) as s:
        a = await s.get(Appointment, moving)
        assert a is not None
        a.ends_at = at(TUE, 10, 15)  # grows into the 10:00 booking
        with pytest.raises(IntegrityError, match="appointment_overlap"):
            await s.flush()
        await s.rollback()


async def test_racing_bookings_cannot_both_win(db: Database) -> None:
    """Two transactions insert overlapping appointments at the same time. The trigger's row lock
    makes the second wait for the first, then see it and fail — no double booking."""
    shop = await make_shop(db)
    first_in = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with db.tenant_session(shop.tenant) as s:
            s.add(_appt(shop, at(TUE, 10)))
            await s.flush()  # holds the resource lock until commit
            first_in.set()
            await release.wait()

    async def second() -> None:
        await first_in.wait()
        async with db.tenant_session(shop.tenant) as s:
            s.add(_appt(shop, at(TUE, 10, 30)))
            await s.flush()

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await first_in.wait()
    await asyncio.sleep(0.3)
    assert not t2.done(), "the second booking must wait for the first"
    release.set()
    await t1
    with pytest.raises(IntegrityError, match="appointment_overlap"):
        await t2
    async with db.tenant_session(shop.tenant) as s:
        assert len((await s.scalars(select(Appointment))).all()) == 1


def _appt(shop: Shop, start: datetime) -> Appointment:
    return Appointment(
        ref=f"R-{uuid.uuid4().hex[:6]}",
        customer_id=shop.customer,
        type_id=shop.consult,
        resource_id=shop.r1,
        starts_at=start,
        ends_at=start + timedelta(minutes=60),
        status="confirmed",
        source="agent",
    )


async def test_concurrent_agent_bookings_of_the_last_slot(db: Database) -> None:
    """Two customers book the same (only) resource's slot through the service at once: one wins,
    the other is told slot_taken; with two resources both win, on different resources."""
    for resources, expect_ok in ((1, 1), (2, 2)):
        shop = await make_shop(db, resources=resources)

        async def attempt(customer: uuid.UUID, shop: Shop = shop) -> str:
            async with db.tenant_session(shop.tenant) as s:
                t = await s.get(AppointmentType, shop.consult)
                assert t is not None
                try:
                    await service.book(
                        s,
                        shop.tenant,
                        service.BookingRequest(
                            customer_id=customer,
                            type_=t,
                            starts_at=at(TUE, 10),
                            source="agent",
                            created_by="agent",
                        ),
                        tz=DUBAI,
                        now=NOW,
                        config=AppointmentsConfig(),
                        only_offered=True,
                    )
                except service.BookingError as exc:
                    return exc.code
                return "ok"

        results = await asyncio.gather(attempt(shop.customer), attempt(shop.other_customer))
        assert results.count("ok") == expect_ok, results
        assert set(results) <= {"ok", "slot_taken"}
        async with db.tenant_session(shop.tenant) as s:
            rows = (await s.scalars(select(Appointment))).all()
        assert len(rows) == expect_ok
        assert len({r.resource_id for r in rows}) == expect_ok
        assert len({r.ref for r in rows}) == expect_ok


# ---------------------------------------------------------------- agent tools


async def test_get_availability_lists_types_then_times(db: Database) -> None:
    shop = await make_shop(db)
    listing = await call(db, shop, "get_availability", {})
    assert [t["name_en"] for t in listing["types"]] == ["Consultation", "Viewing"]
    unknown = await call(db, shop, "get_availability", {"type": "Massage"})
    assert unknown["error"] == "unknown_type"
    result = await call(db, shop, "get_availability", {"type": "consultation"})
    assert [s["starts_at"] for s in result["slots"]] == [
        "2030-01-08T09:00",
        "2030-01-08T09:30",
        "2030-01-08T10:00",
        "2030-01-09T09:00",
        "2030-01-09T09:30",
        "2030-01-09T10:00",
        "2030-01-10T09:00",
        "2030-01-10T09:30",
    ]  # spread: at most three a day, eight in all
    assert result["slots"][0]["when"] == "Tue 8 Jan, 09:00"
    # Arabic name works too; a far range with nothing free widens to the next free times
    assert (await call(db, shop, "get_availability", {"type": "استشارة"}))["slots"]


async def test_get_availability_looks_further_when_the_range_is_full(db: Database) -> None:
    shop = await make_shop(db)
    async with db.tenant_session(shop.tenant) as s:
        s.add(AvailabilityException(resource_id=None, day=TUE))
    r = await call(
        db,
        shop,
        "get_availability",
        {"type": "Consultation", "from_date": "2030-01-08", "to_date": "2030-01-08"},
    )
    assert r["slots"][0]["starts_at"].startswith("2030-01-09")
    assert "next free" in r["note"]


async def test_book_only_an_offered_time_and_only_once(db: Database) -> None:
    shop = await make_shop(db)
    refused = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-08T08:00"}
    )
    assert refused["error"] == "slot_not_available"
    past = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-07T09:00"}
    )
    assert past["error"] == "slot_not_available"
    ok = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-08T10:00"}
    )
    assert ok["ref"] == "A-1001"
    assert ok["status"] == "confirmed"
    assert ok["when"] == "Tue 8 Jan, 10:00"
    again = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-08T10:00"}
    )
    assert again["duplicate"] is True
    assert again["ref"] == "A-1001"
    # the time is gone for everyone else
    taken = await call(
        db,
        shop,
        "book_appointment",
        {"type": "Consultation", "starts_at": "2030-01-08T10:30"},
        customer=shop.other_customer,
    )
    assert taken["error"] == "slot_not_available"
    async with db.tenant_session(shop.tenant) as s:
        a = await s.scalar(select(Appointment))
        assert a is not None
        assert (a.source, a.created_by, a.conversation_id) == ("agent", "agent", shop.conversation)


async def test_team_confirmation_makes_bookings_requests(db: Database) -> None:
    shop = await make_shop(db)
    r = await call(
        db,
        shop,
        "book_appointment",
        {"type": "Consultation", "starts_at": "2030-01-08T10:00"},
        require_team_confirmation=True,
    )
    assert r["status"] == "requested"
    assert "team will confirm" in r["note"]
    # a requested booking holds its time too
    assert "2030-01-08T10:00" not in {
        s["starts_at"]
        for s in (await call(db, shop, "get_availability", {"type": "Consultation"}))["slots"]
    }


async def test_an_on_site_visit_needs_an_address(db: Database) -> None:
    shop = await make_shop(db)
    r = await call(
        db, shop, "book_appointment", {"type": "Viewing", "starts_at": "2030-01-08T10:00"}
    )
    assert r["error"] == "address_needed"
    async with db.tenant_session(shop.tenant) as s:
        c = await s.get(Customer, shop.customer)
        assert c is not None
        c.address_note, c.area = "Villa 12", "Arabian Ranches"
    r = await call(
        db, shop, "book_appointment", {"type": "Viewing", "starts_at": "2030-01-08T10:00"}
    )
    assert r["location_note"] == "Villa 12, Arabian Ranches"


async def test_my_appointments_move_and_cancel(db: Database) -> None:
    shop = await make_shop(db)
    await insert(db, shop, at(TUE, 9), minutes=30)  # someone else's
    booked = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-09T10:00"}
    )
    mine = await call(db, shop, "get_my_appointments", {})
    assert [a["ref"] for a in mine["appointments"]] == [booked["ref"]]

    # another customer cannot touch it
    stranger = await call(
        db, shop, "cancel_appointment", {"ref": booked["ref"]}, customer=shop.other_customer
    )
    assert stranger["error"] == "appointment_not_found"

    moved = await call(
        db,
        shop,
        "reschedule_appointment",
        {"ref": booked["ref"].removeprefix("A-"), "new_starts_at": "2030-01-09T11:00"},
    )
    assert moved["starts_at"] == "2030-01-09T11:00"
    # its own old time does not block moving by half an hour
    moved = await call(
        db,
        shop,
        "reschedule_appointment",
        {"ref": booked["ref"], "new_starts_at": "2030-01-09T10:30"},
    )
    assert moved["starts_at"] == "2030-01-09T10:30"
    bad = await call(
        db,
        shop,
        "reschedule_appointment",
        {"ref": booked["ref"], "new_starts_at": "2030-01-09T12:00"},
    )
    assert bad["error"] == "slot_not_available"

    cancelled = await call(db, shop, "cancel_appointment", {"ref": booked["ref"]})
    assert cancelled["status"] == "cancelled"
    assert (await call(db, shop, "get_my_appointments", {}))["found"] == 0
    again = await call(db, shop, "cancel_appointment", {"ref": booked["ref"]})
    assert again["error"] == "appointment_not_active"


async def test_inside_the_cutoff_the_team_decides(db: Database) -> None:
    shop = await make_shop(db)
    booked = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-08T10:00"}
    )  # 24 h from NOW exactly: inside a 25 h cut-off, outside a 24 h one
    for tool, args in (
        ("cancel_appointment", {"ref": booked["ref"]}),
        ("reschedule_appointment", {"ref": booked["ref"], "new_starts_at": "2030-01-09T10:00"}),
    ):
        r = await call(db, shop, tool, args, cancel_cutoff_hours=25)
        assert r == {"error": "inside_cutoff", "cutoff_hours": 25, "escalated": True}
    async with db.tenant_session(shop.tenant) as s:
        conv = await s.get(Conversation, shop.conversation)
        a = await s.scalar(select(Appointment))
    assert conv is not None
    assert conv.state == "awaiting_human"
    assert a is not None
    assert (a.status, a.starts_at) == ("confirmed", at(TUE, 10))


async def test_a_moved_request_needs_confirming_again(db: Database) -> None:
    shop = await make_shop(db)
    booked = await call(
        db, shop, "book_appointment", {"type": "Consultation", "starts_at": "2030-01-09T10:00"}
    )
    moved = await call(
        db,
        shop,
        "reschedule_appointment",
        {"ref": booked["ref"], "new_starts_at": "2030-01-09T09:00"},
        require_team_confirmation=True,
    )
    assert moved["status"] == "requested"


# ---------------------------------------------------------------- listings


async def _listings(db: Database, tenant: uuid.UUID) -> None:
    async with db.tenant_session(tenant) as s:
        s.add_all(
            [
                Listing(
                    ref=f"JVC-{i}",
                    title=f"{2 if i % 2 else 1} bed in JVC",
                    purpose="rent",
                    property_type="apartment",
                    area="JVC",
                    community="District 12",
                    address_note=f"Tower {i}",
                    bedrooms=2 if i % 2 else 1,
                    price_aed=Decimal(60000 + i * 1000),
                    rent_period="year",
                    status="available",
                    description="Balcony, pool view.",
                )
                for i in range(1, 9)
            ]
            + [
                Listing(
                    ref="JVC-D",
                    title="Draft",
                    purpose="rent",
                    property_type="villa",
                    area="JVC",
                    status="draft",
                ),
                Listing(
                    ref="JVC-S",
                    title="Sold",
                    purpose="sale",
                    property_type="villa",
                    area="JVC",
                    price_aed=Decimal("2000000"),
                    status="sold",
                ),
                Listing(
                    ref="MARINA-1",
                    title="No viewings",
                    purpose="sale",
                    property_type="apartment",
                    area="Dubai Marina",
                    price_aed=Decimal("1500000"),
                    status="available",
                    viewings_enabled=False,
                ),
            ]
        )


async def test_listing_tools_see_only_available_properties(db: Database) -> None:
    shop = await make_shop(db, keys=("listings", "appointments"))
    await _listings(db, shop.tenant)
    r = await call(db, shop, "search_listings", {"purpose": "rent", "area": "jvc"})
    assert r["found"] == 8
    assert len(r["listings"]) == 6
    assert r["listings"][0]["ref"] == "JVC-1"  # cheapest first
    assert "narrow" in r["note"]
    two = await call(
        db, shop, "search_listings", {"purpose": "rent", "bedrooms": 2, "max_price_aed": 64000}
    )
    assert [x["ref"] for x in two["listings"]] == ["JVC-1", "JVC-3"]
    assert (await call(db, shop, "search_listings", {"purpose": "sale", "area": "JVC"}))[
        "found"
    ] == 0
    got = await call(db, shop, "get_listing", {"ref": "jvc-2"})
    assert got["description"] == "Balcony, pool view."
    assert got["price_aed"] == "62000.00"
    for ref in ("JVC-D", "JVC-S", "NOPE"):
        assert (await call(db, shop, "get_listing", {"ref": ref}))[
            "error"
        ] == "listing_not_available"


async def test_a_viewing_is_about_a_listing(db: Database) -> None:
    shop = await make_shop(db, keys=("listings", "appointments"))
    await _listings(db, shop.tenant)
    schema = toolset(enabled_of(shop.keys))["book_appointment"].parameters
    assert "listing_ref" in schema["properties"]
    assert (
        "listing_ref"
        not in toolset(enabled_of(("appointments",)))["book_appointment"].parameters["properties"]
    )

    base = {"type": "Viewing", "starts_at": "2030-01-08T10:00"}
    for ref, code in (("JVC-S", "listing_not_available"), ("MARINA-1", "viewings_not_offered")):
        assert (await call(db, shop, "book_appointment", {**base, "listing_ref": ref}))[
            "error"
        ] == code
    r = await call(db, shop, "book_appointment", {**base, "listing_ref": "jvc-4"})
    assert r["about"] == "JVC-4 · 1 bed in JVC"
    assert r["location_note"] == "Tower 4, District 12, JVC"  # no customer address needed
    async with db.tenant_session(shop.tenant) as s:
        a = await s.scalar(select(Appointment))
        lst = await s.scalar(select(Listing).where(Listing.ref == "JVC-4"))
    assert a is not None
    assert lst is not None
    assert (a.subject_module, a.subject_id) == ("listings", lst.id)


async def test_without_listings_the_parameter_is_ignored(db: Database) -> None:
    shop = await make_shop(db)
    async with db.tenant_session(shop.tenant) as s:
        c = await s.get(Customer, shop.customer)
        assert c is not None
        c.area = "JVC"
    r = await call(
        db,
        shop,
        "book_appointment",
        {"type": "Viewing", "starts_at": "2030-01-08T10:00", "listing_ref": "X"},
    )
    assert r["status"] == "confirmed"
    assert "about" not in r


# ---------------------------------------------------------------- dashboard routes


async def test_dashboard_booking_moving_and_status(dash: DashHarness) -> None:
    shop = await make_shop(dash.db, resources=2)
    headers = await dash.login(shop.tenant, "agent")
    day = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
    body = {
        "customer_id": str(shop.customer),
        "type_id": str(shop.consult),
        "starts_at": f"{day}T10:00",
    }
    r1 = await dash.client.post(
        "/api/v1/m/appointments", headers=headers, json={**body, "resource_id": str(shop.r1)}
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["local"] == f"{day}T10:00"
    # the same resource again: refused by the database guard
    clash = await dash.client.post(
        "/api/v1/m/appointments", headers=headers, json={**body, "resource_id": str(shop.r1)}
    )
    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "slot_taken"
    # without a resource: the next free one
    r2 = await dash.client.post("/api/v1/m/appointments", headers=headers, json=body)
    assert r2.status_code == 201
    assert r2.json()["resource"]["id"] != str(shop.r1)
    # a person may book outside the offered hours (evening)
    late = await dash.client.post(
        "/api/v1/m/appointments", headers=headers, json={**body, "starts_at": f"{day}T19:00"}
    )
    assert late.status_code == 201
    # moving r2 onto r1's time on r1 → 409; status machine
    a2 = r2.json()["id"]
    moved = await dash.client.patch(
        f"/api/v1/m/appointments/{a2}", headers=headers, json={"resource_id": str(shop.r1)}
    )
    assert moved.status_code == 409
    done = await dash.client.patch(
        f"/api/v1/m/appointments/{a2}", headers=headers, json={"status": "completed"}
    )
    assert done.status_code == 200
    assert done.json()["next_statuses"] == []
    back = await dash.client.patch(
        f"/api/v1/m/appointments/{a2}", headers=headers, json={"status": "confirmed"}
    )
    assert back.status_code == 409
    detail = (await dash.client.get(f"/api/v1/m/appointments/{a2}", headers=headers)).json()
    assert [h["action"] for h in detail["history"]] == ["book_appointment", "update_appointment"]
    # the contact panel and Today know about it
    contact = (await dash.client.get(f"/api/v1/contacts/{shop.customer}", headers=headers)).json()
    assert len(contact["modules"]["appointments"]["upcoming"]) == 2
    today = (await dash.client.get("/api/v1/today", headers=headers)).json()
    assert set(today["modules"]["appointments"]) >= {
        "count",
        "next",
        "awaiting_confirmation",
        "items",
    }


async def test_settings_hours_and_roles(dash: DashHarness) -> None:
    shop = await make_shop(dash.db)
    admin = await dash.login(shop.tenant, "admin")
    agent = await dash.login(shop.tenant, "agent")
    url = f"/api/v1/m/appointments/resources/{shop.r1}/hours"
    week = {
        "hours": [
            {"weekday": 0, "start_time": "09:00", "end_time": "12:00"},
            {"weekday": 0, "start_time": "14:00", "end_time": "18:00"},
        ]
    }
    assert (await dash.client.put(url, headers=agent, json=week)).status_code == 403
    r = await dash.client.put(url, headers=admin, json=week)
    assert r.status_code == 200
    assert len((await dash.client.get(url, headers=agent)).json()) == 2
    overlap = {
        "hours": [
            {"weekday": 1, "start_time": "09:00", "end_time": "12:00"},
            {"weekday": 1, "start_time": "11:00", "end_time": "13:00"},
        ]
    }
    r = await dash.client.put(url, headers=admin, json=overlap)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "hours_overlap"
    backwards = {"hours": [{"weekday": 1, "start_time": "12:00", "end_time": "09:00"}]}
    assert (await dash.client.put(url, headers=admin, json=backwards)).status_code == 422
    t = await dash.client.post(
        "/api/v1/m/appointments/types",
        headers=agent,
        json={"name_en": "Call", "duration_min": 15, "location_kind": "phone"},
    )
    assert t.status_code == 403


async def test_listing_routes(dash: DashHarness) -> None:
    tenant = await make_tenant(dash.db, "lst", modules=("listings",))
    agent = await dash.login(tenant, "agent")
    viewer = await dash.login(tenant, "viewer")
    base = {"ref": "JVC-1204", "title": "1 bed", "purpose": "rent", "property_type": "apartment"}
    no_price = await dash.client.post(
        "/api/v1/m/listings", headers=agent, json={**base, "status": "available"}
    )
    assert no_price.status_code == 422
    r = await dash.client.post(
        "/api/v1/m/listings", headers=agent, json={**base, "price_aed": "65000"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"
    lid = r.json()["id"]
    dup = await dash.client.post(
        "/api/v1/m/listings", headers=agent, json={**base, "ref": "jvc-1204"}
    )
    assert dup.status_code == 409
    assert (
        await dash.client.post("/api/v1/m/listings", headers=viewer, json=base)
    ).status_code == 403
    live = await dash.client.patch(
        f"/api/v1/m/listings/{lid}", headers=agent, json={"status": "available"}
    )
    assert live.status_code == 200
    unpriced = await dash.client.patch(
        f"/api/v1/m/listings/{lid}", headers=agent, json={"price_aed": None}
    )
    assert unpriced.status_code == 409
    await dash.client.patch(f"/api/v1/m/listings/{lid}", headers=agent, json={"status": "archived"})
    assert (await dash.client.get("/api/v1/m/listings", headers=viewer)).json()["total"] == 0
    archived = await dash.client.get(
        "/api/v1/m/listings", headers=viewer, params={"status": "archived"}
    )
    assert archived.json()["total"] == 1


# ---------------------------------------------------------------- presets and prompts


async def test_presets(db: Database) -> None:
    law = await make_tenant(db, "law", modules=())
    estate = await make_tenant(db, "estate", modules=())
    async with db.platform_session() as s:
        assert await modules_admin.enable(s, law, ["law_firm"]) == ("appointments", "campaigns")
        assert await modules_admin.enable(s, estate, ["real_estate"]) == (
            "appointments",
            "listings",
            "campaigns",
        )
    async with db.platform_session() as s:
        cfg = (await registry.enabled_for(s, law)).config("appointments")
        assert isinstance(cfg, AppointmentsConfig)
        assert cfg.require_team_confirmation
        other = (await registry.enabled_for(s, estate)).config("appointments")
        assert isinstance(other, AppointmentsConfig)
        assert not other.require_team_confirmation
    # a client's own settings survive the preset being applied again
    async with db.platform_session() as s:
        await modules_admin.enable(
            s,
            law,
            [],
            configs={"appointments": {"require_team_confirmation": True, "slot_minutes": 60}},
        )
        await modules_admin.enable(s, law, ["law_firm"])
    async with db.platform_session() as s:
        cfg = (await registry.enabled_for(s, law)).config("appointments")
    assert isinstance(cfg, AppointmentsConfig)
    assert cfg.slot_minutes == 60


def _vars() -> dict[str, Any]:
    return {
        "agent_name": "Noor",
        "business_name": "Test Co",
        "business_description": "test business",
        "service_areas": "Dubai",
        "cross_sell_category": "snack",
        "customer_block": "Name: x",
        "business_hours": "9-5",
        "knowledge_block": "",
        "today": "2026-09-29",
        "timezone": "Asia/Dubai",
    }


@pytest.mark.parametrize("preset", ["real_estate", "law_firm"])
def test_industry_prompts(preset: str) -> None:
    keys = registry.expand([preset])
    prompt = compose.render_support(keys, **_vars())
    assert "<<" not in prompt
    assert "#. " not in prompt
    assert estimate_tokens(prompt) < Settings.model_fields["system_prompt_token_cap"].default
    assert "- Book, move or cancel appointments" in prompt
    assert (
        "NEVER confirm a booking until you have said back the type, date, time and place" in prompt
    )
    flow = prompt.split("## BOOKING FLOW\n")[1].split("\n\n")[0]
    steps = re.findall(r"^(\d+)\. ", flow, re.M)
    assert steps == [str(i) for i in range(1, len(steps) + 1)]
    assert not re.search(r"coupon|bottle|deliver", prompt, re.I)
    classifier = compose.render_classifier(keys, message_text="hi")
    assert re.search(r"^booking ", classifier, re.M)
    if preset == "real_estate":
        assert "listing_ref" in flow
        assert "## PROPERTY QUESTIONS" in prompt
        assert "NEVER state a property detail, a property price or an appointment time" in prompt
        assert re.search(r"^property ", classifier, re.M)
    else:
        assert "listing" not in prompt.lower()
        assert "NEVER state an appointment time that did not come from a" in prompt
        new_contact = compose.new_contact_text(registry.resolve(keys))
        assert "before you book anything" in new_contact
