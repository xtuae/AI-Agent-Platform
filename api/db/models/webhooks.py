"""Raw webhook payloads (one row per Meta `change`), stored under the resolved tenant."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Text, func
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk


class WebhookEvent(TenantScoped, Base):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    field: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # messages | statuses | template_status | other
    phone_number_id: Mapped[str | None] = mapped_column(Text)
    waba_id: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[uuid.UUID | None]  # set for channels routed by channel_key (Telegram)
    payload: Mapped[dict[str, Any]]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # payload contains customer text; keep it out of tracebacks
        return f"WebhookEvent(id={self.id}, kind={self.kind}, tenant_id={self.tenant_id})"
