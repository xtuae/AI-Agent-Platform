"""Status + template-status webhook jobs: forward-only transitions, campaign counters, RLS."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from api.db.models import (
    Campaign,
    CampaignRecipient,
    Conversation,
    Message,
    MessageTemplate,
    WebhookEvent,
)
from api.db.session import Database
from api.meta.statuses import should_apply
from api.tests.conftest import ChannelPair, messages_change, pnid, wamid
from api.workers.jobs.webhook_events import apply_status_event, apply_template_status_event


@pytest.mark.parametrize(
    ("current", "new", "ok"),
    [
        (None, "sent", True),
        ("sent", "delivered", True),
        ("delivered", "read", True),
        ("sent", "read", True),
        ("read", "delivered", False),
        ("delivered", "sent", False),
        ("sent", "failed", True),
        ("read", "failed", False),
        ("failed", "delivered", False),
        ("sent", "deleted", False),
    ],
)
def test_forward_only(current: str | None, new: str, ok: bool) -> None:
    assert should_apply(current, new) is ok


async def _outbound(
    db: Database, ch: ChannelPair, *, with_campaign: bool
) -> tuple[str, uuid.UUID | None]:
    w = wamid()
    async with db.tenant_session(ch.t.a) as s:
        conv = Conversation(customer_id=ch.t.customer_a, channel_id=ch.a.id)
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                wamid=w,
                direction="out",
                msg_type="template",
                status="sent",
            )
        )
        campaign_id = None
        if with_campaign:
            camp = Campaign(
                name="c", status="sending", approved_by=uuid.uuid4(), approved_at=datetime.now(UTC)
            )
            s.add(camp)
            await s.flush()
            s.add(
                CampaignRecipient(
                    campaign_id=camp.id, customer_id=ch.t.customer_a, status="sent", wamid=w
                )
            )
            campaign_id = camp.id
    return w, campaign_id


async def _status_event(
    db: Database, tenant_id: uuid.UUID, pnid: str, statuses: list[dict[str, Any]]
) -> uuid.UUID:
    async with db.tenant_session(tenant_id) as s:
        ev = WebhookEvent(
            field="messages",
            kind="statuses",
            payload=messages_change(pnid, statuses=statuses)["value"],
        )
        s.add(ev)
        await s.flush()
        return ev.id


async def test_status_progression_updates_message_and_campaign_counters(
    db: Database, channels: ChannelPair
) -> None:
    w, campaign_id = await _outbound(db, channels, with_campaign=True)
    ctx = {"db": db}
    for status in ("delivered", "read", "delivered", "read"):  # includes out-of-order replays
        ev = await _status_event(db, channels.t.a, pnid(channels.a), [{"id": w, "status": status}])
        await apply_status_event(ctx, str(channels.t.a), str(ev))

    async with db.tenant_session(channels.t.a) as s:
        msg = (await s.execute(Message.__table__.select().where(Message.wamid == w))).one()
        assert msg.status == "read"
        camp = await s.get(Campaign, campaign_id)
        assert camp is not None
        assert (camp.delivered_count, camp.read_count) == (1, 1)


async def test_read_without_delivered_counts_both(db: Database, channels: ChannelPair) -> None:
    w, campaign_id = await _outbound(db, channels, with_campaign=True)
    ev = await _status_event(db, channels.t.a, pnid(channels.a), [{"id": w, "status": "read"}])
    await apply_status_event({"db": db}, str(channels.t.a), str(ev))
    async with db.tenant_session(channels.t.a) as s:
        camp = await s.get(Campaign, campaign_id)
        assert camp is not None
        assert (camp.delivered_count, camp.read_count) == (1, 1)


async def test_failed_status_records_error(db: Database, channels: ChannelPair) -> None:
    w, _ = await _outbound(db, channels, with_campaign=False)
    ev = await _status_event(
        db,
        channels.t.a,
        pnid(channels.a),
        [
            {
                "id": w,
                "status": "failed",
                "errors": [{"code": 131047, "title": "Re-engagement message"}],
            }
        ],
    )
    await apply_status_event({"db": db}, str(channels.t.a), str(ev))
    async with db.tenant_session(channels.t.a) as s:
        row = (await s.execute(Message.__table__.select().where(Message.wamid == w))).one()
        assert (row.status, row.error_code, row.error_detail) == (
            "failed",
            "131047",
            "Re-engagement message",
        )


async def test_status_event_cannot_touch_another_tenants_message(
    db: Database, channels: ChannelPair
) -> None:
    w, _ = await _outbound(db, channels, with_campaign=False)  # tenant A's message
    ev = await _status_event(db, channels.t.b, pnid(channels.b), [{"id": w, "status": "read"}])
    result = await apply_status_event({"db": db}, str(channels.t.b), str(ev))
    assert result["messages"] == 0
    async with db.tenant_session(channels.t.a) as s:
        row = (await s.execute(Message.__table__.select().where(Message.wamid == w))).one()
        assert row.status == "sent"


async def test_status_event_is_applied_once(db: Database, channels: ChannelPair) -> None:
    w, _ = await _outbound(db, channels, with_campaign=False)
    ev = await _status_event(db, channels.t.a, pnid(channels.a), [{"id": w, "status": "delivered"}])
    assert (await apply_status_event({"db": db}, str(channels.t.a), str(ev)))["status"] == "ok"
    assert (await apply_status_event({"db": db}, str(channels.t.a), str(ev)))["status"] == "skipped"


async def test_template_status_event_updates_template(db: Database, channels: ChannelPair) -> None:
    async with db.tenant_session(channels.t.a) as s:
        s.add(
            MessageTemplate(
                name="promo",
                language="en",
                category="MARKETING",
                meta_status="PENDING",
                meta_template_id="998877",
            )
        )
        ev = WebhookEvent(
            field="message_template_status_update",
            kind="template_status",
            payload={"event": "APPROVED", "message_template_id": 998877},
        )
        s.add(ev)
        await s.flush()
        ev_id = ev.id
    result = await apply_template_status_event({"db": db}, str(channels.t.a), str(ev_id))
    assert result == {"status": "ok", "updated": 1}
