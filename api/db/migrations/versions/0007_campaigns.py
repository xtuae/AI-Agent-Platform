"""Campaigns module (Phase 4): what the template, campaign and recipient tables were missing.

* message_templates: who made it and when, where it came from (drafted here or imported from
  Meta), when it was submitted, and Meta's rejection reason.
* campaigns: who created it, how each template variable is filled (variable_bindings), when it
  started and finished, and why it is paused. The approval CHECK from 0001 stays the final word.
* campaign_recipients: why a recipient was skipped at send time (opted out since, frequency cap…),
  when they first replied, the variables sent. Indexed for the 7-day frequency cap and for status
  webhooks (wamid).
* tenant_channels.quality_updated_at — when the quality rating was last read from Meta.
* Existing tenants with the orders module (the water preset) get the campaigns module.
* campaigns publish change events like orders (app_notify_change, 0004).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, default: bool = False) -> sa.Column:  # type: ignore[type-arg]
    if default:
        return sa.Column(
            name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
    return sa.Column(name, sa.DateTime(timezone=True))


def upgrade() -> None:
    op.add_column("message_templates", _ts("created_at", default=True))
    op.add_column("message_templates", _ts("updated_at", default=True))
    op.add_column("message_templates", sa.Column("created_by", sa.Text))
    op.add_column(
        "message_templates",
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'drafted'")),
    )
    op.add_column("message_templates", _ts("submitted_at"))
    op.add_column("message_templates", sa.Column("rejected_reason", sa.Text))
    op.create_check_constraint(
        op.f("ck_message_templates_source"), "message_templates", "source in ('drafted','meta')"
    )
    op.create_check_constraint(
        op.f("ck_message_templates_name"),
        "message_templates",
        "name ~ '^[a-z0-9_]+$' AND char_length(name) <= 512",
    )

    op.add_column("campaigns", _ts("created_at", default=True))
    op.add_column("campaigns", sa.Column("created_by", sa.Text))
    op.add_column("campaigns", sa.Column("variable_bindings", pg.JSONB))
    op.add_column("campaigns", _ts("started_at"))
    op.add_column("campaigns", _ts("finished_at"))
    op.add_column("campaigns", sa.Column("paused_reason", sa.Text))
    op.add_column(
        "campaigns",
        sa.Column("recipient_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "campaigns",
        sa.Column("skipped_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "campaigns",
        sa.Column("failed_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        op.f("ck_campaigns_throttle"), "campaigns", "throttle_per_minute between 1 and 1000"
    )

    op.add_column("campaign_recipients", _ts("created_at", default=True))
    op.add_column("campaign_recipients", sa.Column("skip_reason", sa.Text))
    op.add_column("campaign_recipients", _ts("replied_at"))
    op.add_column("campaign_recipients", sa.Column("variables", pg.JSONB))
    op.create_index(
        "ix_campaign_recipients_customer_sent",
        "campaign_recipients",
        ["tenant_id", "customer_id", "sent_at"],
    )
    op.create_index("ix_campaign_recipients_wamid", "campaign_recipients", ["wamid"])
    op.create_index(
        "ix_campaign_recipients_campaign_status", "campaign_recipients", ["campaign_id", "status"]
    )

    op.add_column("tenant_channels", _ts("quality_updated_at"))

    op.execute(
        "INSERT INTO tenant_modules (tenant_id, module_key) "
        "SELECT tenant_id, 'campaigns' FROM tenant_modules WHERE module_key = 'orders' "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(
        "CREATE TRIGGER trg_campaigns_notify AFTER INSERT OR UPDATE ON campaigns "
        "FOR EACH ROW EXECUTE FUNCTION app_notify_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_campaigns_notify ON campaigns")
    op.execute("DELETE FROM tenant_modules WHERE module_key = 'campaigns'")
    op.drop_column("tenant_channels", "quality_updated_at")
    op.drop_index("ix_campaign_recipients_campaign_status", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_wamid", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_customer_sent", table_name="campaign_recipients")
    for col in ("variables", "replied_at", "skip_reason", "created_at"):
        op.drop_column("campaign_recipients", col)
    op.drop_constraint("throttle", "campaigns", type_="check")
    for col in (
        "failed_count",
        "skipped_count",
        "recipient_count",
        "paused_reason",
        "finished_at",
        "started_at",
        "variable_bindings",
        "created_by",
        "created_at",
    ):
        op.drop_column("campaigns", col)
    op.drop_constraint(op.f("ck_message_templates_name"), "message_templates", type_="check")
    op.drop_constraint("source", "message_templates", type_="check")
    for col in (
        "rejected_reason",
        "submitted_at",
        "source",
        "created_by",
        "updated_at",
        "created_at",
    ):
        op.drop_column("message_templates", col)
