"""Print a DPA-ready description of how the platform processes one tenant's data (Markdown).

    python -m api.scripts.dpa --slug acme > docs/dpa/acme.md

Built from what the tenant actually has switched on (its modules, its channel, its settings), so
the document describes this deployment, not a generic one. Review it before sending: it states
facts about the system, not legal positions; the DPA itself is for the lawyers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select

from api.config import get_settings
from api.db.models import Tenant, TenantChannel, TenantSettings
from api.db.session import Database
from api.modules import registry

# What each module adds to the personal data processed. Core (always on) is listed separately.
MODULE_DATA: Final[dict[str, str]] = {
    "catalog": "No personal data (the business's products and prices).",
    "orders": "Orders placed by customers: items, quantities, delivery address note, delivery date "
    "and status, order history.",
    "coupons": "Prepaid coupon books per customer: purchase date, bottles remaining, expiry.",
    "appointments": "Appointments booked by or for customers: time, type, notes, status.",
    "listings": "No personal data (the business's property listings).",
    "campaigns": "Marketing consent and its evidence; which customers received which approved "
    "template message, delivery status and replies.",
}

SUBPROCESSORS: Final = [
    ("Meta Platforms Ireland Ltd (WhatsApp Business Cloud API)", "Carries every message to and "
     "from "
     "customers. The client's own WhatsApp Business Account; Meta bills the client directly."),
    ("Google LLC (Gemini API)", "Generates the assistant's replies and transcribes voice notes. "
     "Receives the conversation text needed for the reply, under Google's paid API terms "
     "(use a paid key: free-tier terms differ)."),
    ("OpenRouter Inc.", "Fallback route to the same model family, used only when Gemini is "
     "unavailable."),
    ("Amazon Web Services, Inc.", "Virtual server (EC2, us-east-1, United States) hosting the "
     "application and database."),
    ("Cloudflare Inc. (DNS)", "Resolves the platform's domain names; no message content passes "
     "through it."),
    ("Cloudflare Inc. (R2) or Backblaze Inc. (B2)", "Stores nightly backups, encrypted before "
     "upload with a key held by HMH Labz off the server."),
]  # fmt: skip


async def render(slug: str) -> str:
    db = Database(get_settings())
    try:
        async with db.platform_session() as s:
            tenant = await s.scalar(select(Tenant).where(Tenant.slug == slug))
            if tenant is None:
                raise ValueError(f"no tenant with slug {slug!r}")
            ts = await s.get(TenantSettings, tenant.id)
            channel = await s.scalar(
                select(TenantChannel).where(TenantChannel.tenant_id == tenant.id).limit(1)
            )
            enabled = await registry.enabled_for(s, tenant.id)
    finally:
        await db.dispose()

    number = channel.display_phone if channel and channel.display_phone else "not yet connected"
    label = (ts.contact_label if ts and ts.contact_label else "Customers").lower()
    lines = [
        f"# Data processing description — {tenant.legal_name or tenant.name}",
        "",
        f"Processor: HMH Labz LLP. Controller: {tenant.legal_name or tenant.name}"
        + (f" (contract {tenant.contract_ref})" if tenant.contract_ref else "")
        + f". Generated {datetime.now(UTC):%d %B %Y} from the live configuration.",
        "",
        "## Purpose",
        "",
        f"An automated WhatsApp assistant answers the controller's {label} on its WhatsApp "
        "number, records what they ask for, and hands conversations to the controller's staff "
        "when needed. The controller's staff use a web dashboard to see and act on the same data.",
        "",
        "## Data subjects and personal data",
        "",
        f"Data subjects: the controller's {label} who message its WhatsApp number or are "
        "imported from its records; the controller's staff who sign in to the dashboard.",
        "",
        "Always processed:",
        "",
        "- WhatsApp number and WhatsApp profile name; name, area, emirate, address note and "
        "language when given.",
        "- Message content in both directions; voice notes are transcribed and only the "
        "transcript is kept (audio files are not stored).",
        "- Marketing consent status with its evidence (the exact wording shown, time, source).",
        "- Staff accounts: email, name, role, sign-in times; an audit log of changes they make.",
        "",
        "Processed because of the services enabled for this controller:",
        "",
        *[
            f"- **{m.name}** — {MODULE_DATA.get(m.key, 'See module description.')}"
            for m in enabled.modules
        ],
        "",
        "No special-category data is requested. Payment card data is never handled.",
        "",
        "## Where it is processed",
        "",
        "- Application and database: one virtual server operated by HMH Labz (see sub-processors).",
        f"- WhatsApp number: {number}, on the controller's own WhatsApp Business Account.",
        "- Model provider requests are processed by the providers listed below, which may process "
        "outside the controller's country.",
        "",
        "## Sub-processors",
        "",
        *[f"- **{name}** — {what}" for name, what in SUBPROCESSORS],
        "",
        "## Security measures",
        "",
        "- Each controller's data is isolated in the database by row-level security enforced by "
        "PostgreSQL itself; the application connects as a role that cannot bypass it.",
        "- WhatsApp access tokens are encrypted at rest; no token or message content is written "
        "to logs.",
        "- Dashboard sign-in with per-person accounts and roles; HMH Labz staff use a separate "
        "console with two-factor authentication.",
        "- TLS for all traffic; the server firewall admits only web traffic and key-only SSH "
        "(provisioning in RUNBOOK.md).",
        "- Nightly encrypted backups kept 30 days; restores are tested.",
        "- Opting out (STOP) is honoured immediately and cannot be overridden by a campaign.",
        "",
        "## Retention",
        "",
        "- Conversation and customer records are kept for the life of the contract, then deleted "
        "or returned on the controller's instruction. No automatic shorter period is applied "
        "today; one can be agreed and configured.",
        "- Backups expire after 30 days, so deleted data leaves the backups within 30 days.",
        "",
        "## Data subject requests",
        "",
        "The controller can find and correct a person's record, and opt them out, from the "
        "dashboard. "
        "Deletion on request is carried out by HMH Labz on the controller's written instruction.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--slug", required=True)
    args = ap.parse_args(argv)
    try:
        print(asyncio.run(render(args.slug)))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
