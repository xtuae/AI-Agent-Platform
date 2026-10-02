"""Backups by hand (the scheduler runs one nightly).

    python -m api.scripts.backup keygen                  # once: new age key pair
    python -m api.scripts.backup run                     # back up now (uses BACKUP_* settings)
    python -m api.scripts.backup list
    python -m api.scripts.backup restore --identity-file key.txt --target-db agents_restored
    python -m api.scripts.backup restore --key <key> --identity-file key.txt --target-db x \\
                                         --admin-url postgresql://postgres:…@host:5432/postgres

`keygen` prints the PUBLIC key for BACKUP_AGE_RECIPIENT and writes the PRIVATE key to a file.
Keep that file off the server (password manager + offline copy): without it no backup can be
read, which is the point. `restore` always creates a NEW database and verifies every table's
row count against the backup's manifest; it never touches a live database. --local-dir reads
and writes a directory instead of the bucket.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pyrage

from api.config import get_settings
from api.ops.backup import BackupError, LocalStorage, Storage, backup, latest, restore
from api.ops.jobs import storage_from


def _storage(args: argparse.Namespace) -> Storage:
    return LocalStorage(Path(args.local_dir)) if args.local_dir else storage_from(get_settings())


def _keygen(path: Path) -> None:
    if path.exists():
        raise BackupError(f"{path} exists; refusing to overwrite a private key")
    ident = pyrage.x25519.Identity.generate()
    path.write_text(str(ident) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"Private key written to {path} (mode 600). Move it OFF this machine.")
    print(f"BACKUP_AGE_RECIPIENT={ident.to_public()}")


async def _main(args: argparse.Namespace, identity: str | None) -> int:
    settings = get_settings()
    storage = _storage(args)
    if args.cmd == "list":
        for key, modified in storage.list(settings.backup_prefix + "/"):
            print(f"{modified:%Y-%m-%d %H:%M}  {key}")
        return 0
    if args.cmd == "run":
        if settings.backup_database_url is None or not settings.backup_age_recipient:
            raise BackupError("BACKUP_DATABASE_URL and BACKUP_AGE_RECIPIENT are required")
        r = await backup(
            dsn=settings.backup_database_url.get_secret_value(),
            recipient=settings.backup_age_recipient,
            storage=storage,
            prefix=settings.backup_prefix,
            retention_days=settings.backup_retention_days,
        )
        print(
            f"Backed up {r.tables} tables, {r.rows} rows → {r.key} ({r.size} bytes); "
            f"removed {len(r.deleted)} old"
        )
        return 0
    # restore
    assert identity is not None
    key = latest(storage, settings.backup_prefix) if args.key in (None, "latest") else args.key
    admin = args.admin_url or (
        settings.backup_database_url.get_secret_value() if settings.backup_database_url else None
    )
    if not admin:
        raise BackupError("--admin-url (a superuser) is required")
    r2 = await restore(
        key=key,
        identity=identity,
        storage=storage,
        admin_dsn=admin,
        target_db=args.target_db,
        owners=not args.no_owner,
    )
    print(f"Restored {key} into {r2.database}: {r2.tables} tables, {r2.rows} rows")
    if r2.mismatches:
        for t, (want, got) in sorted(r2.mismatches.items()):
            print(f"  MISMATCH {t}: backup {want}, restored {got}")
        return 1
    print("Row counts match the backup's manifest for every table.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen")
    kg.add_argument("--out", default="backup-age-key.txt")
    for name in ("run", "list"):
        sub.add_parser(name).add_argument("--local-dir")
    rs = sub.add_parser("restore")
    rs.add_argument("--key", default="latest")
    rs.add_argument("--identity-file", required=True)
    rs.add_argument("--target-db", required=True)
    rs.add_argument("--admin-url")
    rs.add_argument("--local-dir")
    rs.add_argument("--no-owner", action="store_true", help="restore without role ownership")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keygen":
            _keygen(Path(args.out))
            return 0
        identity = (
            Path(args.identity_file).read_text(encoding="utf-8") if args.cmd == "restore" else None
        )
        return asyncio.run(_main(args, identity))
    except (BackupError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
