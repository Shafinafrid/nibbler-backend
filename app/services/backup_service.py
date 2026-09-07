"""Encrypted, application-managed PostgreSQL backups.

Railway's native volume-backup mutation is unavailable to the current project
role, so the production scheduler makes one logical ``pg_dump`` archive per UTC
day. Archives are validated with ``pg_restore --list`` before they are uploaded
to the private, versioned S3 bucket. S3 server-side encryption is requested
explicitly even though the bucket also encrypts by default.

No database password is placed in the process argument list or logs. It is
passed to libpq through the child process environment only.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from botocore.exceptions import ClientError
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.services.s3_service import S3Service


logger = logging.getLogger(__name__)
settings = get_settings()

BACKUP_PREFIX = "system-backups/postgres/"


class DatabaseBackupError(RuntimeError):
    """A backup could not be produced, validated, uploaded, or retained."""


def _postgres_environment(database_url: str) -> tuple[dict[str, str], str]:
    """Return a libpq environment and database name without exposing secrets."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise DatabaseBackupError("DATABASE_URL is not a valid SQLAlchemy URL") from exc

    if url.get_backend_name() not in {"postgresql", "postgres"}:
        raise DatabaseBackupError("database backup requires PostgreSQL")
    if not url.host or not url.database:
        raise DatabaseBackupError("PostgreSQL backup URL is missing host or database")

    env = os.environ.copy()
    env.update({
        "PGHOST": str(url.host),
        "PGPORT": str(url.port or 5432),
        "PGDATABASE": str(url.database),
        "PGUSER": str(url.username or "postgres"),
    })
    if url.password is not None:
        env["PGPASSWORD"] = str(url.password)

    query = dict(url.query)
    sslmode = query.get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = str(sslmode)
    return env, str(url.database)


def _run_checked(command: list[str], *, env: Optional[dict[str, str]] = None) -> None:
    try:
        subprocess.run(
            command,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30 * 60,
        )
    except FileNotFoundError as exc:
        raise DatabaseBackupError(f"required backup executable is missing: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DatabaseBackupError(f"{command[0]} exceeded the 30 minute safety limit") from exc
    except subprocess.CalledProcessError as exc:
        # pg tools do not include the connection password in normal stderr.
        # Still cap the provider text so an operational error cannot flood logs.
        detail = (exc.stderr or "").strip()[:500]
        raise DatabaseBackupError(f"{command[0]} failed: {detail or 'unknown error'}") from exc


def _object_exists(client, bucket: str, key: str) -> bool:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
        return int(response.get("ContentLength", 0)) > 0
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _expire_old_backups(client, bucket: str, today: date, retention_days: int) -> int:
    """Delete current objects past retention; bucket versioning protects mistakes."""
    cutoff = datetime.combine(
        today - timedelta(days=max(1, retention_days)),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    removed = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=BACKUP_PREFIX):
        for obj in page.get("Contents", []):
            modified = obj.get("LastModified")
            key = obj.get("Key")
            if key and modified and modified < cutoff:
                client.delete_object(Bucket=bucket, Key=key)
                removed += 1
    return removed


def create_daily_postgres_backup(
    *,
    now: Optional[datetime] = None,
    database_url: Optional[str] = None,
    s3: Optional[S3Service] = None,
) -> dict:
    """Create today's validated archive, idempotently, and enforce retention."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    service = s3 or S3Service()
    key = f"{BACKUP_PREFIX}{current:%Y/%m/%d}/nibbler-production.dump"

    if _object_exists(service.client, service.bucket, key):
        return {"status": "already_exists", "key": key, "bytes": 0, "expired": 0}

    pg_env, database_name = _postgres_environment(database_url or settings.database_url)
    with tempfile.TemporaryDirectory(prefix="nibbler-pg-backup-") as temp_dir:
        archive = Path(temp_dir) / "nibbler.dump"
        _run_checked([
            "pg_dump",
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            f"--file={archive}",
            database_name,
        ], env=pg_env)

        if not archive.is_file() or archive.stat().st_size <= 0:
            raise DatabaseBackupError("pg_dump produced an empty archive")
        _run_checked(["pg_restore", "--list", str(archive)])

        try:
            with archive.open("rb") as body:
                service.client.upload_fileobj(
                    body,
                    service.bucket,
                    key,
                    ExtraArgs={
                        "ContentType": "application/octet-stream",
                        "ServerSideEncryption": "AES256",
                        "Metadata": {
                            "created-at-utc": current.isoformat(),
                            "format": "pg-dump-custom",
                        },
                    },
                )
        except Exception as exc:
            raise DatabaseBackupError("validated database archive could not be uploaded") from exc

        size = archive.stat().st_size

    try:
        expired = _expire_old_backups(
            service.client,
            service.bucket,
            current.date(),
            settings.database_backup_retention_days,
        )
    except Exception as exc:
        # The backup itself is already durable. Retention failure is observable
        # but must not turn a successful recovery point into a reported failure.
        logger.exception("Database backup retention pass failed")
        expired = -1

    logger.info("Database backup completed: key=%s bytes=%d expired=%d", key, size, expired)
    return {"status": "created", "key": key, "bytes": size, "expired": expired}
