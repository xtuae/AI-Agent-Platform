"""Import every model so Base.metadata is complete (Alembic, tests)."""

from api.db.models.conversations import Conversation, Message
from api.db.models.customers import Customer
from api.db.models.knowledge import KnowledgeChunk
from api.db.models.metering import AuditLog, UsageDaily
from api.db.models.platform import (
    AuthRefreshToken,
    PlatformUser,
    Tenant,
    TenantChannel,
    TenantModule,
    TenantSettings,
    TenantUser,
)
from api.db.models.pricing import MetaRate
from api.db.models.webhooks import WebhookEvent

# Module-owned tables. Every deployment has every table (one migration chain); a module being
# switched on for a tenant is a tenant_modules row. Re-exported so core code and Alembic see one
# complete metadata.
from api.modules.appointments.models import (
    Appointment,
    AppointmentResource,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
)
from api.modules.campaigns.models import Campaign, CampaignRecipient, MessageTemplate
from api.modules.catalog.models import Product
from api.modules.coupons.models import CouponBook, CouponPackage
from api.modules.listings.models import Listing
from api.modules.orders.models import Order

__all__ = [
    "Appointment",
    "AppointmentResource",
    "AppointmentType",
    "AuditLog",
    "AuthRefreshToken",
    "AvailabilityException",
    "AvailabilityRule",
    "Campaign",
    "CampaignRecipient",
    "Conversation",
    "CouponBook",
    "CouponPackage",
    "Customer",
    "KnowledgeChunk",
    "Listing",
    "Message",
    "MessageTemplate",
    "MetaRate",
    "Order",
    "PlatformUser",
    "Product",
    "Tenant",
    "TenantChannel",
    "TenantModule",
    "TenantSettings",
    "TenantUser",
    "UsageDaily",
    "WebhookEvent",
]
