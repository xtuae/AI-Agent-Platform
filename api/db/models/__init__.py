"""Import every model so Base.metadata is complete (Alembic, tests)."""

from api.db.models.conversations import Conversation, Message
from api.db.models.customers import Customer, CustomerIdentity
from api.db.models.knowledge import KnowledgeChunk
from api.db.models.metering import AuditLog, MetaStatementLine, MetaStatementMonth, UsageDaily
from api.db.models.optin import OptinVisit
from api.db.models.platform import (
    AuthRefreshToken,
    OptinLink,
    PlatformUser,
    Reimbursement,
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
    "CustomerIdentity",
    "KnowledgeChunk",
    "Listing",
    "Message",
    "MessageTemplate",
    "MetaRate",
    "MetaStatementLine",
    "MetaStatementMonth",
    "OptinLink",
    "OptinVisit",
    "Order",
    "PlatformUser",
    "Product",
    "Reimbursement",
    "Tenant",
    "TenantChannel",
    "TenantModule",
    "TenantSettings",
    "TenantUser",
    "UsageDaily",
    "WebhookEvent",
]
