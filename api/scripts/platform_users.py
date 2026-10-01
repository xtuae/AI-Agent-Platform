"""Manage HMH Labz staff accounts for the platform console.

    python -m api.scripts.platform_users list
    python -m api.scripts.platform_users add --email ops@hmhlabz.com --role ops --name "Ops"
    python -m api.scripts.platform_users reset-totp --email ops@hmhlabz.com
    python -m api.scripts.platform_users password --email ops@hmhlabz.com
    python -m api.scripts.platform_users disable --email ops@hmhlabz.com

Roles: owner (everything, including recording reimbursements), ops (also pulls Meta statements),
support (read only). `add` and `reset-totp` print the authenticator setup once — as a QR code in
the terminal and as an otpauth:// link. It is not stored anywhere else in readable form, so scan
it straight away. Passwords are typed at a prompt (never an argument: shell history).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

import pyotp
import segno
from sqlalchemy import func, select

from api.config import get_settings
from api.core.passwords import hash_password
from api.db.models import PlatformUser
from api.db.session import Database
from api.platform.auth import ROLES

ISSUER = "HMH Labz console"
MIN_PASSWORD = 12


def _password() -> str:
    first = getpass.getpass("Password (12+ characters): ")
    if len(first) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if getpass.getpass("Again: ") != first:
        raise ValueError("passwords do not match")
    return first


def _show_totp(email: str, secret: str) -> None:
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)
    print("\nScan this with an authenticator app (shown once):\n")
    segno.make(uri, error="m").terminal(compact=True)
    print(f"\nOr enter this link manually:\n{uri}\n")


async def _find(db: Database, email: str) -> PlatformUser:
    async with db.platform_session() as s:
        user = await s.scalar(
            select(PlatformUser).where(func.lower(PlatformUser.email) == email.lower())
        )
    if user is None:
        raise ValueError(f"no staff account {email}")
    return user


async def _run(args: argparse.Namespace) -> None:
    db = Database(get_settings())
    try:
        if args.cmd == "list":
            async with db.platform_session() as s:
                users = (await s.scalars(select(PlatformUser).order_by(PlatformUser.email))).all()
            for u in users:
                state = "active" if u.is_active else "disabled"
                totp = "totp" if u.totp_secret else "NO TOTP"
                print(f"{u.email:<36} {u.role:<8} {state:<9} {totp}")
            return
        if args.cmd == "add":
            secret = pyotp.random_base32()
            async with db.platform_session() as s:
                s.add(
                    PlatformUser(
                        email=args.email.lower(),
                        name=args.name,
                        role=args.role,
                        password_hash=await asyncio.to_thread(hash_password, args.password),
                        totp_secret=secret,
                    )
                )
            print(f"Added {args.email} ({args.role}).")
            _show_totp(args.email, secret)
            return
        user = await _find(db, args.email)
        async with db.platform_session() as s:
            row = await s.get(PlatformUser, user.id)
            assert row is not None
            if args.cmd == "reset-totp":
                row.totp_secret = pyotp.random_base32()
                _show_totp(row.email, row.totp_secret)
            elif args.cmd == "password":
                row.password_hash = await asyncio.to_thread(hash_password, args.password)
                print("Password changed.")
            elif args.cmd == "disable":
                row.is_active = False
                print(f"Disabled {row.email}. Their console session ends on the next request.")
            elif args.cmd == "enable":
                row.is_active = True
                print(f"Enabled {row.email}.")
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("--email", required=True)
    add.add_argument("--role", required=True, choices=ROLES)
    add.add_argument("--name")
    for name in ("reset-totp", "password", "disable", "enable"):
        sub.add_parser(name).add_argument("--email", required=True)
    args = ap.parse_args(argv)
    try:
        # prompted before the event loop starts: never a command-line argument (shell history)
        args.password = _password() if args.cmd in ("add", "password") else None
        asyncio.run(_run(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
