"""F03 regression: backup secrets, validation, encryption and idempotency."""

import os
import tempfile
from datetime import datetime, timezone

import hermetic  # noqa: F401
from botocore.exceptions import ClientError

from app.services import backup_service as backups


failures = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


class FakePaginator:
    def paginate(self, **kwargs):
        return [{"Contents": []}]


class FakeClient:
    def __init__(self, exists=False):
        self.exists = exists
        self.uploads = []

    def head_object(self, **kwargs):
        if self.exists:
            return {"ContentLength": 42}
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def upload_fileobj(self, body, bucket, key, ExtraArgs):
        self.uploads.append((bucket, key, body.read(), ExtraArgs))

    def get_paginator(self, name):
        return FakePaginator()

    def delete_object(self, **kwargs):
        raise AssertionError("empty retention fixture must not delete")


class FakeS3:
    def __init__(self, exists=False):
        self.client = FakeClient(exists)
        self.bucket = "private-test-bucket"


commands = []
real_run = backups._run_checked


def fake_run(command, env=None):
    commands.append((list(command), dict(env or {})))
    if command[0] == "pg_dump":
        filename = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        with open(filename, "wb") as out:
            out.write(b"validated-custom-archive")


backups._run_checked = fake_run
try:
    url = "postgresql://backup_user:do-not-print-this@db.example.invalid:5432/nibbler?sslmode=require"
    fake_s3 = FakeS3()
    result = backups.create_daily_postgres_backup(
        now=datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc),
        database_url=url,
        s3=fake_s3,
    )
finally:
    backups._run_checked = real_run

check("a new recovery point is created", result["status"] == "created", result)
check("pg_dump and pg_restore validation both run", [c[0][0] for c in commands] == ["pg_dump", "pg_restore"])
flat_args = " ".join(arg for command, _ in commands for arg in command)
check("database password never appears in process arguments", "do-not-print-this" not in flat_args)
check("password is passed only through the child libpq environment", commands[0][1].get("PGPASSWORD") == "do-not-print-this")
check("TLS mode is preserved for pg_dump", commands[0][1].get("PGSSLMODE") == "require")
upload = fake_s3.client.uploads[0]
check("archive upload explicitly requests AES256 encryption", upload[3].get("ServerSideEncryption") == "AES256")
check("archive key is UTC-date scoped", upload[1] == "system-backups/postgres/2026/09/06/nibbler-production.dump", upload[1])
check("the validated bytes are what S3 receives", upload[2] == b"validated-custom-archive")

existing = FakeS3(exists=True)
skipped = backups.create_daily_postgres_backup(
    now=datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc),
    database_url="postgresql://u:p@db.example.invalid/nibbler",
    s3=existing,
)
check("same-day retries are idempotent", skipped["status"] == "already_exists" and not existing.client.uploads)

if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("RESULT: ALL PASS")
