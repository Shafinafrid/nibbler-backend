"""F03: real pg_dump -> validation -> upload bytes -> isolated restore drill."""

import atexit
import glob
import os
import shutil
import socket
import subprocess
import tempfile
from datetime import datetime, timezone

import hermetic  # noqa: F401
import psycopg2

from app.services.backup_service import create_daily_postgres_backup


def pg_bin(name):
    for directory in sorted(glob.glob("/opt/homebrew/Cellar/postgresql@*/*/bin"), reverse=True):
        if os.path.exists(os.path.join(directory, "postgres")):
            return os.path.join(directory, name)
    return name


root = tempfile.mkdtemp(prefix="nibbler-backup-restore-")
data = os.path.join(root, "data")
sock = os.path.join(root, "sock")
os.makedirs(sock)
started = False


def cleanup():
    if started:
        subprocess.run([pg_bin("pg_ctl"), "-D", data, "-m", "fast", "-w", "stop"],
                       capture_output=True)
    shutil.rmtree(root, ignore_errors=True)


atexit.register(cleanup)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()

env = dict(os.environ, LC_ALL="C", LANG="C")
subprocess.run([pg_bin("initdb"), "-D", data, "-U", "postgres", "-A", "trust",
                "-E", "UTF8", "--no-sync"], check=True, capture_output=True, env=env)
subprocess.run([pg_bin("pg_ctl"), "-D", data, "-o",
                f"-k {sock} -h 127.0.0.1 -p {port}", "-l", os.path.join(root, "pg.log"),
                "-w", "start"], check=True, capture_output=True, env=env)
started = True

admin = psycopg2.connect(host="127.0.0.1", port=port, user="postgres", dbname="postgres")
admin.autocommit = True
with admin.cursor() as cur:
    cur.execute("CREATE DATABASE source_db")
    cur.execute("CREATE DATABASE restored_db")
admin.close()

source = psycopg2.connect(host="127.0.0.1", port=port, user="postgres", dbname="source_db")
with source.cursor() as cur:
    cur.execute("CREATE TABLE recovery_probe (id integer PRIMARY KEY, value text NOT NULL)")
    cur.execute("INSERT INTO recovery_probe VALUES (7, 'verified recovery')")
source.commit()
source.close()


class EmptyPaginator:
    def paginate(self, **_kwargs):
        return [{"Contents": []}]


class MemoryClient:
    def __init__(self):
        self.archive = None

    def head_object(self, **_kwargs):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def upload_fileobj(self, body, _bucket, _key, ExtraArgs):
        assert ExtraArgs["ServerSideEncryption"] == "AES256"
        self.archive = body.read()

    def get_paginator(self, _name):
        return EmptyPaginator()


class MemoryS3:
    bucket = "private-test-bucket"

    def __init__(self):
        self.client = MemoryClient()


s3 = MemoryS3()
url = f"postgresql://postgres@127.0.0.1:{port}/source_db"
result = create_daily_postgres_backup(
    now=datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc),
    database_url=url,
    s3=s3,
)
assert result["status"] == "created" and s3.client.archive

archive_path = os.path.join(root, "downloaded.dump")
with open(archive_path, "wb") as handle:
    handle.write(s3.client.archive)
restore_result = subprocess.run([
    pg_bin("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error",
    f"--dbname=postgresql://postgres@127.0.0.1:{port}/restored_db", archive_path,
], capture_output=True, text=True)
if restore_result.returncode:
    raise RuntimeError(f"pg_restore failed: {restore_result.stderr.strip()[:1000]}")

restored = psycopg2.connect(host="127.0.0.1", port=port, user="postgres", dbname="restored_db")
with restored.cursor() as cur:
    cur.execute("SELECT id, value FROM recovery_probe")
    row = cur.fetchone()
restored.close()

ok = row == (7, "verified recovery")
print(f"  [{'PASS' if ok else 'FAIL'}] validated uploaded bytes restore into an isolated database")
cleanup()
started = False
if not ok:
    raise SystemExit(1)
print("RESULT: ALL PASS")
