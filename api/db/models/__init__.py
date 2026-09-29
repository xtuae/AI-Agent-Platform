"""Import every model so Base.metadata is complete (Alembic, tests)."""

from api.db.models.campaigns import Campaign, CampaignRecipient, MessageTemplate
from api.db.models.catalog import CouponPackage
from api.db.models.commerce import Order, Product
from api.db.models.conversations import Conversation, Message
from api.db.models.customers import CouponBook, Customer
from api.db.models.knowledge import KnowledgeChunk
from api.db.models.metering import AuditLog, UsageDaily
from api.db.models.platform import (
    AuthRefreshToken,
    PlatformUser,
    Tenant,
    TenantChannel,
    TenantSettings,
    TenantUser,
)
from api.db.models.pricing import MetaRate
from api.db.models.webhooks import WebhookEvent

__all__ = [
    "AuditLog",
    "AuthRefreshToken",
    "Campaign",
    "CampaignRecipient",
    "Conversation",
    "CouponBook",
    "CouponPackage",
    "Customer",
    "KnowledgeChunk",
    "Message",
    "MessageTemplate",
    "MetaRate",
    "Order",
    "PlatformUser",
    "Product",
    "Tenant",
    "TenantChannel",
    "TenantSettings",
    "TenantUser",
    "UsageDaily",
    "WebhookEvent",
]
