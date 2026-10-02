"""Encrypted database backups and verified restores (Phase 6: "an untested backup is not a backup").

Backup (nightly, scheduler; or `python -m api.scripts.backup run`):
 1. One REPEATABLE READ transaction exports a snapshot; every table's row count is taken inside
    it and pg_dump dumps exactly that snapshot (--snapshot), so the counts in the manifest are the
    counts in the dump — a restore can be verified to the row.
 2. pg_dump -Fc runs as the backup role: read-only (pg_read_all_data) with BYPASSRLS. RLS is
    FORCED on tenant tables, so any other role would dump them empty.
 3. The dump is encrypted with age to a PUBLIC key (BACKUP_AGE_RECIPIENT). The private key is not
    on the server: a stolen server or bucket cannot read a backup.
 4. Uploaded to S3-compatible storage (Cloudflare R2 / Backblaze B2) with its manifest; objects
    older than BACKUP_RETENTION_DAYS are deleted.
 5. Success is stamped in Redis (the watchdog alerts after 26 h without one); failure alerts.

Restore (`python -m api.scripts.backup restore`): download → decrypt with the private key →
create a fresh database → pg_restore → compare every table's row count with the manifest.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pyrage

from api.core.logging import get_logger

log = get_logger(__name__)

TABLES_SQL: Final = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name
"""
SUFFIX: Final = ".dump.age"
MANIFEST: Final = ".manifest.json"
SUBPROCESS_TIMEOUT_S: Final = 3600


class BackupError(RuntimeError):
    pass


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str) -> list[tuple[str, datetime]]: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    """A directory standing in for a bucket (tests, or a box with no object storage yet)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise BackupError("key escapes the storage root")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        out = []
        for p in self.root.rglob("*"):
            key = p.relative_to(self.root).as_posix()
            if p.is_file() and key.startswith(prefix):
                out.append((key, datetime.fromtimestamp(p.stat().st_mtime, UTC)))
        return sorted(out)

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)


class S3Storage:
    """Cloudflare R2 / Backblaze B2 through the S3 API."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "auto",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                connect_timeout=10,
                read_timeout=120,
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    def put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        body: bytes = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return body

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        out: list[tuple[str, datetime]] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            out.extend((o["Key"], o["LastModified"]) for o in page.get("Contents", []))
        return sorted(out)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)


def libpq(dsn: str) -> str:
    """postgresql+asyncpg://… → postgresql://… for pg_dump / asyncpg."""
    parts = urlsplit(dsn)
    return urlunsplit(("postgresql", *parts[1:]))


def with_database(dsn: str, name: str) -> str:
    parts = urlsplit(libpq(dsn))
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


async def _counts(conn: Any) -> dict[str, int]:
    """Row count of every table, inside the caller's transaction (the dump's snapshot)."""
    out: dict[str, int] = {}
    for (name,) in await conn.fetch(TABLES_SQL):
        safe = name.replace('"', '""')
        # identifier from the catalogue, double-quoted with quotes escaped: not user input
        out[name] = int(await conn.fetchval(f'SELECT count(*) FROM public."{safe}"'))  # noqa: S608
    return out


async def _run(*cmd: str, env: dict[str, str] | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), SUBPROCESS_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise BackupError(f"{cmd[0]} timed out") from None
    if proc.returncode != 0:
        # stderr of pg tools names objects, never row data; keep it short anyway
        raise BackupError(f"{Path(cmd[0]).name} failed: {err.decode(errors='replace')[-400:]}")
    return out


@dataclass(frozen=True)
class BackupResult:
    key: str
    size: int
    tables: int
    rows: int
    deleted: list[str]


def _keypair_check(recipient: str) -> Any:
    try:
        return pyrage.x25519.Recipient.from_str(recipient)
    except Exception as exc:
        raise BackupError("BACKUP_AGE_RECIPIENT is not a valid age public key") from exc


async def backup(
    *,
    dsn: str,
    recipient: str,
    storage: Storage,
    prefix: str,
    retention_days: int,
    pg_dump: str = "pg_dump",
    now: datetime | None = None,
) -> BackupResult:
    rec = _keypair_check(recipient)
    now = now or datetime.now(UTC)
    url = libpq(dsn)
    db_name = urlsplit(url).path.lstrip("/") or "postgres"
    key = f"{prefix}/{db_name}/{now:%Y/%m}/{db_name}-{now:%Y%m%dT%H%M%SZ}"

    conn = await asyncpg.connect(url, timeout=10)
    try:
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        try:
            snapshot = await conn.fetchval("SELECT pg_export_snapshot()")
            counts = await _counts(conn)
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "db.dump"
                await _run(
                    pg_dump, "--format=custom", f"--snapshot={snapshot}", f"--file={out}", url
                )
                plain = await asyncio.to_thread(out.read_bytes)
        finally:
            await tx.rollback()
    finally:
        await conn.close()

    if not plain.startswith(b"PGDMP"):
        raise BackupError("pg_dump produced something that is not a custom-format dump")
    encrypted: bytes = await asyncio.to_thread(pyrage.encrypt, plain, [rec])
    manifest = {
        "created_at": now.isoformat(),
        "database": db_name,
        "format": "pg_dump custom, age-encrypted",
        "size_plain": len(plain),
        "counts": counts,
    }
    # the storage clients are blocking (boto3): keep them off the event loop
    await asyncio.to_thread(storage.put, key + SUFFIX, encrypted)
    await asyncio.to_thread(storage.put, key + MANIFEST, json.dumps(manifest, indent=1).encode())

    deleted: list[str] = []
    cutoff = now - timedelta(days=retention_days)
    for k, modified in await asyncio.to_thread(storage.list, f"{prefix}/{db_name}/"):
        if modified < cutoff:
            await asyncio.to_thread(storage.delete, k)
            deleted.append(k)
    log.info("backup_done", key=key, size=len(encrypted), tables=len(counts), deleted=len(deleted))
    return BackupResult(key, len(encrypted), len(counts), sum(counts.values()), deleted)


def latest(storage: Storage, prefix: str) -> str:
    keys = [k for k, _ in storage.list(prefix + "/") if k.endswith(SUFFIX)]
    if not keys:
        raise BackupError("no backups found")
    return keys[-1].removesuffix(SUFFIX)  # names sort by timestamp


@dataclass(frozen=True)
class RestoreResult:
    database: str
    tables: int
    rows: int
    mismatches: dict[str, tuple[int, int]]  # table → (manifest, restored)


async def restore(
    *,
    key: str,
    identity: str,
    storage: Storage,
    admin_dsn: str,
    target_db: str,
    pg_restore: str = "pg_restore",
    owners: bool = True,
) -> RestoreResult:
    """Restore `key` into a NEW database `target_db` (created here; refuses an existing one) and
    verify its row counts. `admin_dsn` must be a superuser (extensions, ownership)."""
    try:
        ident = pyrage.x25519.Identity.from_str(identity.strip())
    except Exception as exc:
        raise BackupError("not a valid age private key") from exc
    manifest: dict[str, Any] = json.loads(await asyncio.to_thread(storage.get, key + MANIFEST))
    blob = await asyncio.to_thread(storage.get, key + SUFFIX)
    try:
        plain: bytes = await asyncio.to_thread(pyrage.decrypt, blob, [ident])
    except Exception as exc:
        raise BackupError("cannot decrypt: wrong key, or the file is damaged") from exc

    if not target_db.replace("_", "").isalnum():
        raise BackupError("target database name must be letters, digits and underscores")
    admin = await asyncpg.connect(libpq(admin_dsn), timeout=10)
    try:
        if await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target_db):
            raise BackupError(f"database {target_db} already exists; restore only into a new one")
        await admin.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await admin.close()

    target = with_database(admin_dsn, target_db)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "db.dump"
        await asyncio.to_thread(f.write_bytes, plain)
        flags = ["--exit-on-error"] + ([] if owners else ["--no-owner", "--no-privileges"])
        await _run(pg_restore, *flags, f"--dbname={target}", str(f))

    conn = await asyncpg.connect(target, timeout=10)
    try:
        restored = await _counts(conn)
    finally:
        await conn.close()
    expected: dict[str, int] = manifest["counts"]
    mismatches = {
        t: (expected.get(t, -1), restored.get(t, -1))
        for t in set(expected) | set(restored)
        if expected.get(t) != restored.get(t)
    }
    log.info("restore_done", database=target_db, tables=len(restored), mismatches=len(mismatches))
    return RestoreResult(target_db, len(restored), sum(restored.values()), mismatches)
