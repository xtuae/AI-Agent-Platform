"""Meta-only sends: templates, and the MetaClient for a WhatsApp channel.

Free-form replies for every channel live in api.channels.outbound; the names below are re-exported
so existing imports keep working.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from api.channels.outbound import (
    ChannelNotConfiguredError,
    LLMUsage,
    OutsideServiceWindowError,
    _prepare,
    _record,
    send_text_reply,
)
from api.config import Settings
from api.core.crypto import decrypt_secret
from api.db.models import MessageTemplate, TenantChannel
from api.db.session import Database
from api.meta.client import MetaClient
from api.metering import PricingCategory

__all__ = [
    "ChannelNotConfiguredError",
    "LLMUsage",
    "OutsideServiceWindowError",
    "client_for_channel",
    "send_template",
    "send_text_reply",
]

_TEMPLATE_CATEGORY: dict[str, PricingCategory] = {
    "MARKETING": "marketing",
    "UTILITY": "utility",
    "AUTHENTICATION": "authentication",
}


def client_for_channel(
    channel: TenantChannel, http: httpx.AsyncClient, settings: Settings
) -> MetaClient:
    if channel.kind != "whatsapp" or channel.phone_number_id is None:
        raise ChannelNotConfiguredError(f"channel {channel.id} is not a WhatsApp number")
    if channel.access_token_encrypted is None:
        raise ChannelNotConfiguredError(f"channel {channel.id} has no access token")
    return MetaClient(
        http=http,
        access_token=decrypt_secret(channel.access_token_encrypted),
        phone_number_id=channel.phone_number_id,
        api_version=settings.meta_graph_api_version,
        base_url=settings.meta_graph_base_url,
        max_retries=settings.meta_max_retries,
        timeout=httpx.Timeout(
            settings.meta_http_timeout_s, connect=settings.meta_connect_timeout_s
        ),
        media_max_bytes=settings.meta_media_max_bytes,
    )


async def send_template(
    db: Database,
    client: MetaClient,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    template: MessageTemplate,
    components: list[dict[str, Any]] | None = None,
    rendered_body: str | None = None,
) -> uuid.UUID:
    category = _TEMPLATE_CATEGORY.get((template.category or "").upper())
    if category is None:
        raise ValueError(f"template {template.name!r} has no billable category")
    prepared = await _prepare(db, tenant_id, conversation_id, category=category)
    result = await client.send_template(prepared.to, template.name, template.language, components)
    return await _record(
        db,
        tenant_id,
        conversation_id,
        prepared,
        result,
        msg_type="template",
        body=rendered_body,
        template_name=template.name,
    )
