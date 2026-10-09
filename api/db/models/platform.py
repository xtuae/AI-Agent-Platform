"""Platform tables (01_architecture §5.1). No RLS — these define tenancy itself."""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, uuid_pk


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status in ('trial','active','suspended','churned')", name="status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'trial'"))
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Dubai'"))
    locale_default: Mapped[str] = mapped_column(Text, server_default=text("'en'"))
    contract_ref: Mapped[str | None] = mapped_column(Text)
    service_start: Mapped[date | None]
    free_months_until: Mapped[date | None]
    meta_charges_borne_by_us_until: Mapped[date | None]
    monthly_fee_aed: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # margin view
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantChannel(Base):
    """The webhook routing table: a WhatsApp phone_number_id, or a Telegram channel_key, →
    tenant_id. `kind` says which; the columns of the other kind are NULL (a CHECK enforces it)."""

    __tablename__ = "tenant_channels"
    __table_args__ = (
        # Target for composite FKs from tenant tables (conversations.channel_id).
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("kind in ('whatsapp','telegram')", name="kind"),
        CheckConstraint(
            "(kind = 'whatsapp' AND phone_number_id IS NOT NULL AND telegram_bot_id IS NULL)"
            " OR (kind = 'telegram' AND phone_number_id IS NULL AND telegram_bot_id IS NOT NULL)",
            name="kind_fields",
        ),
        CheckConstraint("channel_key ~ '^[A-Za-z0-9_-]{16,64}$'", name="channel_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(Text, server_default=text("'whatsapp'"))
    # Unguessable, in the per-channel webhook URL. Not a credential; never logged.
    channel_key: Mapped[str] = mapped_column(
        Text, unique=True, default=lambda: secrets.token_urlsafe(24)
    )
    waba_id: Mapped[str | None] = mapped_column(Text)
    phone_number_id: Mapped[str | None] = mapped_column(Text, unique=True)
    display_phone: Mapped[str | None] = mapped_column(Text)
    # Fernet ciphertext of the sending credential (Meta access token / Telegram bot token).
    # NEVER plaintext, never logged.
    access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None]
    webhook_verify_token: Mapped[str | None] = mapped_column(Text)
    quality_rating: Mapped[str | None] = mapped_column(Text)
    messaging_limit_tier: Mapped[str | None] = mapped_column(Text)
    quality_updated_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    telegram_bot_id: Mapped[str | None] = mapped_column(Text, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(Text)
    # sha256 of the secret_token given to Telegram's setWebhook; the secret itself is not kept
    webhook_secret_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    webhook_set_at: Mapped[datetime | None]
    webhook_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # keep ciphertext out of tracebacks and debug output
        return (
            f"TenantChannel(id={self.id}, tenant_id={self.tenant_id}, kind={self.kind}, "
            f"active={self.is_active})"
        )


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    llm_provider: Mapped[str] = mapped_column(Text, server_default=text("'gemini'"))
    llm_model_chat: Mapped[str] = mapped_column(Text, server_default=text("'gemini-3-flash'"))
    llm_model_classify: Mapped[str] = mapped_column(
        Text, server_default=text("'gemini-2.5-flash-lite'")
    )
    monthly_message_cap_aed: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    monthly_token_cap: Mapped[int | None] = mapped_column(Integer)
    business_hours: Mapped[dict[str, Any] | None]
    escalation_phone: Mapped[str | None] = mapped_column(Text)
    agent_persona: Mapped[dict[str, Any] | None]
    feature_flags: Mapped[dict[str, Any] | None]
    # What the dashboard calls the people this business talks to: Customers, Clients, Leads…
    contact_label: Mapped[str | None] = mapped_column(Text)


class TenantModule(Base):
    """Which capability modules a tenant has (api/modules). Written by HMH Labz only
    (api.scripts.modules); a client admin may change `config`, never `enabled`."""

    __tablename__ = "tenant_modules"
    __table_args__ = (CheckConstraint("module_key ~ '^[a-z][a-z0-9_]*$'", name="key"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    module_key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    enabled_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PlatformUser(Base):
    """HMH Labz staff."""

    __tablename__ = "platform_users"
    __table_args__ = (CheckConstraint("role in ('owner','ops','support')", name="role"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    # base32; required to sign in (the console sees every tenant). Never logged or returned.
    totp_secret: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"PlatformUser(id={self.id}, role={self.role})"


class TenantUser(Base):
    """Client-side dashboard logins."""

    __tablename__ = "tenant_users"
    __table_args__ = (
        CheckConstraint("role in ('admin','agent','viewer')", name="role"),
        UniqueConstraint("tenant_id", "email"),
        UniqueConstraint("tenant_id", "id"),
        Index("ix_tenant_users_email_lower", func.lower(text("email"))),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    email: Mapped[str] = mapped_column(Text)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    # Deactivated, never deleted: audit_log and messages.sent_by keep pointing at a real row.
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"TenantUser(id={self.id}, tenant_id={self.tenant_id}, role={self.role})"


class AuthRefreshToken(Base):
    """Dashboard refresh tokens. Platform table: a refresh arrives with only a cookie, before any
    tenant is known. Only sha256(token) is stored. Rotated on every use; presenting a rotated
    token again revokes the whole family (stolen-token detection)."""

    __tablename__ = "auth_refresh_tokens"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_users.tenant_id", "tenant_users.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by: Mapped[uuid.UUID | None]

    def __repr__(self) -> str:
        return f"AuthRefreshToken(id={self.id}, user_id={self.user_id})"


class OptinLink(Base):
    """A tenant's consent page behind a QR code / short URL (/q/<code>). Platform table: the
    public page resolves `code` to a tenant before any tenant context exists."""

    __tablename__ = "optin_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("code ~ '^[A-Za-z0-9]{6,16}$'", name="code"),
        CheckConstraint("source ~ '^[a-z][a-z0-9_]{1,31}$'", name="source"),
        CheckConstraint("language in ('en','ar')", name="language"),
        CheckConstraint("char_length(wording) BETWEEN 20 AND 1000", name="wording"),
        CheckConstraint("char_length(prefill) BETWEEN 2 AND 200", name="prefill"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    code: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text)  # "Van 3 sticker" — for the team, never shown
    source: Mapped[str] = mapped_column(Text)  # customers.source / evidence source: 'qr_van'…
    language: Mapped[str] = mapped_column(Text, server_default=text("'en'"))
    heading: Mapped[str] = mapped_column(Text)
    wording: Mapped[str] = mapped_column(Text)  # the exact consent wording shown
    prefill: Mapped[str] = mapped_column(Text)  # the WhatsApp message; the code is appended
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(Text)


class Reimbursement(Base):
    """HMH Labz paid a tenant back for Meta charges in one borne-by-HMH service month."""

    __tablename__ = "reimbursements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "service_month"),
        CheckConstraint("service_month >= 1", name="month"),
        CheckConstraint("amount_aed >= 0", name="amount"),
        CheckConstraint("basis in ('meta_statement','metered')", name="basis"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    service_month: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[date]
    period_end: Mapped[date]  # inclusive
    amount_aed: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    basis: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime] = mapped_column(server_default=func.now())
    recorded_by: Mapped[str] = mapped_column(Text)
