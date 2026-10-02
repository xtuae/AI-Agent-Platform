"""Rotate a WhatsApp channel's Meta access token (HMH Labz staff).

    python -m api.scripts.channel_token list
    python -m api.scripts.channel_token set --phone-number-id 1234567890 [--expires 2027-01-31]

`set` reads the new system-user token from a hidden prompt (or stdin when piped) — never from a
command-line argument, which would land in shell history. It is checked against Meta (the phone
number's status is read with it) before it replaces the old one, stored Fernet-encrypted, and
never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import UTC, date, datetime, time

import httpx
from sqlalchemy import select

from api.config import get_settings
from api.core.crypto import encrypt_secret
from api.db.models import Tenant, TenantChannel
from api.db.session import Database
from api.meta.client import MetaAPIError, MetaClient


async def _run(args: argparse.Namespace, token: str | None) -> int:
    settings = get_settings()
    db = Database(settings)
    try:
        async with db.platform_session() as s:
            if args.cmd == "list":
                rows = (
                    await s.execute(
                        select(Tenant.slug, TenantChannel).join(
                            Tenant, Tenant.id == TenantChannel.tenant_id
                        )
                    )
                ).all()
                for slug, ch in rows:
                    exp = (
                        f"{ch.token_expires_at:%Y-%m-%d}"
                        if ch.token_expires_at
                        else "no expiry recorded"
                    )
                    has = "token set" if ch.access_token_encrypted else "NO TOKEN"
                    print(f"{slug:<24} {ch.phone_number_id:<18} {has:<10} {exp}")
                return 0
            ch = await s.scalar(
                select(TenantChannel).where(TenantChannel.phone_number_id == args.phone_number_id)
            )
        if ch is None:
            print("error: no channel with that phone_number_id", file=sys.stderr)
            return 2
        assert token
        async with httpx.AsyncClient() as http:
            client = MetaClient(
                http=http,
                access_token=token,
                phone_number_id=ch.phone_number_id,
                api_version=settings.meta_graph_api_version,
                base_url=settings.meta_graph_base_url,
            )
            try:
                status = await client.phone_status()
            except MetaAPIError as exc:
                print(
                    f"error: Meta rejected the new token ({exc}); nothing changed", file=sys.stderr
                )
                return 1
        async with db.platform_session() as s:
            row = await s.get(TenantChannel, ch.id)
            assert row is not None
            row.access_token_encrypted = encrypt_secret(token)
            row.token_expires_at = (
                datetime.combine(args.expires, time(0), UTC) if args.expires else None
            )
        quality = status.quality_rating or "unknown"
        print(f"Token replaced for {ch.phone_number_id} (quality {quality}).")
        return 0
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    st = sub.add_parser("set")
    st.add_argument("--phone-number-id", required=True)
    st.add_argument("--expires", type=date.fromisoformat, help="token expiry date, if it has one")
    args = ap.parse_args(argv)
    token = None
    if args.cmd == "set":
        token = (
            getpass.getpass("New token: ") if sys.stdin.isatty() else sys.stdin.readline()
        ).strip()
        if not token:
            print("error: empty token", file=sys.stderr)
            return 2
    return asyncio.run(_run(args, token))


if __name__ == "__main__":
    sys.exit(main())
