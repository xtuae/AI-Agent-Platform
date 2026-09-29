"""Template status bookkeeping from Meta's message_template_status_update webhook."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import MessageTemplate
from api.webhooks.payloads import TemplateStatusValue


async def apply_template_status(session: AsyncSession, value: TemplateStatusValue) -> int:
    """Set meta_status on the matching template (tenant-scoped session). Returns rows updated.

    The campaign sender re-checks status against Meta at send time (02 §4.4); this only keeps
    the dashboard current."""
    result = await session.execute(
        update(MessageTemplate)
        .where(MessageTemplate.meta_template_id == str(value.message_template_id))
        .values(meta_status=value.event.upper())
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
