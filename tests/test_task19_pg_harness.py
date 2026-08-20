"""
Task 19 — checked-in, reproducible real-PostgreSQL harness for the
deployment-readiness fail-hard contract. Everything SQLite genuinely cannot
prove about this task lives here, exactly the way test_task2_pg_harness.py
proves Task 2's own Postgres-only claims — same reasons: `_run_migrations()`'s
fail-hard/advisory-lock behavior is Postgres-only BY DESIGN (see its
docstring in app/database.py), and SQLite has no pg_advisory_lock, no
information_schema/pg_catalog, and no real concurrent-connection race to
reproduce a lock actually serializing two workers.

    .venv/bin/python tests/test_task19_pg_harness.py

By default this creates, uses, and tears down a fully disposable local
Postgres 16+ cluster (initdb + pg_ctl on a throwaway Unix socket directory,
removed at the end) — identical bootstrap to test_task2_pg_harness.py,
duplicated rather than imported (this repo's own established convention;
every PG-harness file inlines its own bootstrap). To point it at an
already-running disposable instance instead, set:

    PG_HARNESS_DATABASE_URL=postgresql://user:pass@host:port/dbname

— the target database must be one this script may freely DROP/CREATE on and
write arbitrary test data into; never point this at a real environment.

  PG1 — create_tables() on a fresh real Postgres database succeeds (the
        actual production code path, not a simulation) and
        verify_required_schema() independently confirms zero missing items.
  PG2 — a genuinely broken required migration (the notes table dropped out
        from under `ALTER TABLE notes ADD COLUMN ...`) makes _run_migrations()
        RAISE on Postgres — proving the fail-hard behavior fires for a REAL
        failure using the real, unmodified statement list, not a synthetic
        stand-in for one.
  PG3 — after PG2's failure, the module's advisory lock is provably
        released (not leaked) — a second call can still acquire it.
  PG4 — MIGRATIONS_ADVISORY_LOCK_KEY actually serializes two concurrent
        callers: a second real connection holding that exact lock blocks
        _run_migrations() from proceeding until it releases (same
        real-concurrency proof pattern as Task 2's PG7).
  PG5 — verify_required_schema() catches a required UNIQUE INDEX (created
        via `CREATE UNIQUE INDEX`, not `ADD CONSTRAINT`) genuinely dropped
        from Postgres — proving the pg_indexes half of the introspection.
  PG5b — verify_required_schema() catches a required UNIQUE CONSTRAINT
        (an ORM `UniqueConstraint`, backed by `information_schema.table_
        constraints`/`pg_constraint`, NOT `pg_indexes`) genuinely dropped
        — proving the OTHER half. Added after an independent audit found
        the original query compared bare constraint names with no schema
        or table scoping (would have missed a same-named constraint
        collision in another schema, or silently passed if matched
        against the wrong table) — this section exercises exactly the
        code path that bug lived in, which PG5 alone did not.
  PG6 — verify_required_schema() catches a required table genuinely
        dropped from Postgres.
"""
import os, sys, shutil, socket, subprocess, tempfile, threading, time, atexit

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")


# ═════════════════════════════════════════════════════════════════════════
# Disposable cluster bootstrap — identical to test_task2_pg_harness.py
# ═════════════════════════════════════════════════════════════════════════
_cluster_dir = None
_pg_started_here = False


def _pg_bin(name: str) -> str:
    import glob
    for path in sorted(glob.glob("/opt/homebrew/Cellar/postgresql@*/*/bin"), reverse=True):
        if os.path.exists(os.path.join(path, "postgres")) and os.path.exists(os.path.join(path, name)):
            return os.path.join(path, name)
    for path in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(path, "postgres")) and os.path.exists(os.path.join(path, name)):
            return os.path.join(path, name)
    return name


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _bootstrap_disposable_cluster() -> str:
    global _cluster_dir, _pg_started_here
    _cluster_dir = tempfile.mkdtemp(prefix="nibbler-pg-harness-task19-")
    data_dir = os.path.join(_cluster_dir, "data")
    sock_dir = os.path.join(_cluster_dir, "sock")
    os.makedirs(sock_dir, exist_ok=True)
    port = _free_port()

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"

    try:
        subprocess.run(
            [_pg_bin("initdb"), "-D", data_dir, "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-sync"],
            check=True, capture_output=True, env=env, text=True,
        )
        subprocess.run(
            [_pg_bin("pg_ctl"), "-D", data_dir, "-o",
             f"-k {sock_dir} -h '' -p {port}", "-l", os.path.join(_cluster_dir, "pg.log"),
             "-w", "start"],
            check=True, capture_output=True, env=env, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"  [bootstrap] FAILED: {e}\n  stdout: {e.stdout}\n  stderr: {e.stderr}")
        raise
    _pg_started_here = True
    subprocess.run(
        [_pg_bin("psql"), "-h", sock_dir, "-p", str(port), "-U", "postgres", "-d", "postgres",
         "-c", "CREATE DATABASE nibbler_pg_harness_task19"],
        check=True, capture_output=True, env=env, text=True,
    )
    return f"postgresql://postgres@/nibbler_pg_harness_task19?host={sock_dir}&port={port}"


def _teardown_disposable_cluster():
    if not _cluster_dir:
        return
    if _pg_started_here:
        data_dir = os.path.join(_cluster_dir, "data")
        try:
            env = dict(os.environ); env["LC_ALL"] = "C"; env["LANG"] = "C"
            subprocess.run([_pg_bin("pg_ctl"), "-D", data_dir, "-m", "fast", "-w", "stop"],
                            capture_output=True, env=env, timeout=30)
        except Exception as e:
            print(f"  [teardown] pg_ctl stop raised: {e}")
    try:
        shutil.rmtree(_cluster_dir, ignore_errors=True)
    except Exception as e:
        print(f"  [teardown] cluster dir removal raised: {e}")
    print(f"  [teardown] disposable cluster stopped and removed ({_cluster_dir})")


external_url = os.environ.get("PG_HARNESS_DATABASE_URL")
if external_url:
    print("Using externally-provided disposable database (PG_HARNESS_DATABASE_URL set).")
    database_url = external_url
else:
    print("Bootstrapping a fully disposable local Postgres cluster...")
    atexit.register(_teardown_disposable_cluster)
    database_url = _bootstrap_disposable_cluster()
    print(f"  cluster ready at {database_url}")

# NOT hermetic.py: that module forcibly overrides any non-sqlite:// DATABASE_URL.
for _name in (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "PINECONE_API_KEY", "VOYAGE_API_KEY", "MIXPANEL_TOKEN", "RESEND_API_KEY",
    "EXPO_ACCESS_TOKEN", "REVENUECAT_SECRET_API_KEY", "REVENUECAT_WEBHOOK_SECRET",
    "OPENAI_API_KEY", "OPENAI_LLM_API_KEY", "ANTHROPIC_LLM_API_KEY",
    "QWEN_API_KEY", "QWEN_BASE_URL",
    "FIREBASE_PRIVATE_KEY", "FIREBASE_PRIVATE_KEY_ID", "FIREBASE_CLIENT_EMAIL",
    "FIREBASE_CLIENT_ID", "BUG_DRIVE_FOLDER_ID",
):
    os.environ[_name] = ""
os.environ["CLAUDE_API_KEY"] = "t"
os.environ["FIREBASE_PROJECT_ID"] = "test-project"
os.environ["SECRET_KEY"] = "test-secret-key-not-a-real-one-000000"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = database_url
_cwd_tempdir = tempfile.mkdtemp(prefix="nibbler-pg-harness-task19-cwd-")
os.chdir(_cwd_tempdir)
atexit.register(lambda: shutil.rmtree(_cwd_tempdir, ignore_errors=True))

from sqlalchemy import text, create_engine

import app.database as dbmod
from app.database import (
    create_tables, engine as db_engine, verify_required_schema,
    MIGRATIONS_ADVISORY_LOCK_KEY,
)

try:
    # ═════════════════════════════════════════════════════════════════════
    section("PG1 — create_tables() on a fresh real Postgres DB")
    # ═════════════════════════════════════════════════════════════════════
    # The exact production call path (main.py's lifespan): if this raises,
    # the harness itself crashes here — which IS the assertion for the
    # happy path (a working schema must boot cleanly).
    create_tables()
    check("create_tables() completed without raising", True)
    check("_boot_schema_verified is True after a clean boot", dbmod._boot_schema_verified is True)

    ok, missing = verify_required_schema()
    check("verify_required_schema() independently confirms zero missing items", ok, str(missing))

    # ═════════════════════════════════════════════════════════════════════
    section("PG2 — a genuinely broken required migration makes _run_migrations() RAISE")
    # ═════════════════════════════════════════════════════════════════════
    # Drop a real table one of the (unmodified, real) migration statements
    # targets — "ALTER TABLE notes ADD COLUMN IF NOT EXISTS daily_bite_id
    # VARCHAR" — so re-running _run_migrations() hits a genuine Postgres
    # error ("relation notes does not exist"), not a synthetic stand-in.
    with db_engine.connect() as conn:
        conn.execute(text("DROP TABLE notes CASCADE"))
        conn.commit()

    raised = None
    try:
        dbmod._run_migrations()
    except Exception as e:
        raised = e
    check("_run_migrations() raised after a real required statement failed",
          raised is not None and isinstance(raised, RuntimeError))
    check("the raised error names the broken statement",
          raised is not None and "notes" in str(raised).lower(), str(raised) if raised else "")

    # Repair for the sections below.
    with db_engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE notes (id VARCHAR PRIMARY KEY, user_id VARCHAR, book_id VARCHAR, "
            "card_index INTEGER, text VARCHAR, created_at TIMESTAMP)"
        ))
        conn.commit()
    dbmod._run_migrations()  # re-apply the columns/indexes notes needs, now that it exists again
    ok_repair, missing_repair = verify_required_schema()
    check("schema is healthy again after repairing the dropped table", ok_repair, str(missing_repair))

    # ═════════════════════════════════════════════════════════════════════
    section("PG3 — the advisory lock is released even after a failure (not leaked)")
    # ═════════════════════════════════════════════════════════════════════
    # If PG2's `finally: pg_advisory_unlock` didn't run, this SECOND
    # top-level call would either hang (blocking on itself, in a real
    # multi-connection scenario) or — since it's the SAME session here —
    # Postgres would let it re-acquire (session-level locks are
    # re-entrant per session), so the real proof is: a DIFFERENT
    # connection can take the lock immediately after, with no lingering
    # holder.
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as probe:
        got_lock = probe.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATIONS_ADVISORY_LOCK_KEY}
        ).scalar()
        check("a fresh connection can immediately acquire the migrations lock "
              "(PG2's failure path released it)", got_lock is True)
        if got_lock:
            probe.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATIONS_ADVISORY_LOCK_KEY})

    # ═════════════════════════════════════════════════════════════════════
    section("PG4 — MIGRATIONS_ADVISORY_LOCK_KEY genuinely serializes two callers")
    # ═════════════════════════════════════════════════════════════════════
    # Real two-connection concurrency, same pattern as Task 2's PG7 — a
    # second OS-level connection holds the exact lock _run_migrations()
    # needs; _run_migrations() (on a real OS thread) must BLOCK until that
    # holder releases, not silently proceed as if unlocked.
    holder_conn = db_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    holder_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATIONS_ADVISORY_LOCK_KEY})

    migration_done = threading.Event()
    migration_error = []

    def _run_migrations_in_thread():
        try:
            dbmod._run_migrations()
        except Exception as e:
            migration_error.append(e)
        finally:
            migration_done.set()

    t = threading.Thread(target=_run_migrations_in_thread, daemon=True)
    t.start()
    blocked_as_expected = not migration_done.wait(timeout=1.5)
    check("_run_migrations() is still BLOCKED 1.5s later while the lock is held elsewhere",
          blocked_as_expected)

    holder_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATIONS_ADVISORY_LOCK_KEY})
    holder_conn.close()
    completed_after_release = migration_done.wait(timeout=10)
    check("_run_migrations() completes promptly once the lock is released", completed_after_release)
    check("the unblocked _run_migrations() call itself succeeded (schema was healthy)",
          completed_after_release and not migration_error, str(migration_error))
    t.join(timeout=5)

    # ═════════════════════════════════════════════════════════════════════
    section("PG5 — verify_required_schema() catches a dropped required UNIQUE constraint")
    # ═════════════════════════════════════════════════════════════════════
    with db_engine.connect() as conn:
        # This was created via CREATE UNIQUE INDEX (not ADD CONSTRAINT) —
        # DROP INDEX is the correct removal, proving the pg_indexes half of
        # the introspection query, not just pg_constraint.
        conn.execute(text("DROP INDEX IF EXISTS uq_saved_bites_user_bite"))
        conn.commit()
    ok5, missing5 = verify_required_schema()
    check("dropped unique index is detected as missing", not ok5)
    check("missing list names the specific constraint",
          "constraint:saved_bites.uq_saved_bites_user_bite" in missing5, str(missing5))
    # Repair.
    with db_engine.connect() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_bites_user_bite ON saved_bites (user_id, bite_id)"
        ))
        conn.commit()

    # ═════════════════════════════════════════════════════════════════════
    section("PG5b — verify_required_schema() catches a dropped required UNIQUE CONSTRAINT "
            "(not an index), and correctly ignores a same-named object on the WRONG table")
    # ═════════════════════════════════════════════════════════════════════
    # uq_chat_turn_user_turn is an ORM `UniqueConstraint` (app/models/user_data.py),
    # created inline by Base.metadata.create_all() as a real ADD-CONSTRAINT-style
    # object — backed by information_schema.table_constraints/pg_constraint, NOT
    # pg_indexes. This is the exact code path the pre-fix query got wrong: it
    # compared bare names with no schema/table scoping, so a same-named
    # constraint anywhere in the database (even on a totally different table)
    # would have made a REAL missing constraint invisible to this check.
    with db_engine.connect() as conn:
        conn.execute(text("ALTER TABLE chat_turns DROP CONSTRAINT uq_chat_turn_user_turn"))
        conn.commit()
    ok5b, missing5b = verify_required_schema()
    check("dropped unique CONSTRAINT (not index) is detected as missing", not ok5b)
    check("missing list names the specific constraint",
          "constraint:chat_turns.uq_chat_turn_user_turn" in missing5b, str(missing5b))

    # Now the false-negative reproduction: create a SAME-NAMED unique
    # constraint on an unrelated table. Under the pre-fix (name-only,
    # unscoped) query this alone would have satisfied `name not in
    # existing_relnames` and silently hidden the fact that chat_turns
    # itself still has no such constraint — the exact bug the audit found.
    with db_engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE task19_decoy (id VARCHAR PRIMARY KEY, x VARCHAR, "
            "CONSTRAINT uq_chat_turn_user_turn UNIQUE (x))"
        ))
        conn.commit()
    ok5b_decoy, missing5b_decoy = verify_required_schema()
    check("a same-named constraint on the WRONG table does NOT hide the real gap "
          "(table-scoping actually matters, not just existence-by-name)",
          not ok5b_decoy and "constraint:chat_turns.uq_chat_turn_user_turn" in missing5b_decoy,
          str(missing5b_decoy))
    with db_engine.connect() as conn:
        conn.execute(text("DROP TABLE task19_decoy"))
        conn.commit()

    # Repair.
    with db_engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE chat_turns ADD CONSTRAINT uq_chat_turn_user_turn UNIQUE (user_id, turn_id)"
        ))
        conn.commit()
    ok5b_repaired, missing5b_repaired = verify_required_schema()
    check("schema is healthy again after re-adding the real constraint",
          ok5b_repaired, str(missing5b_repaired))

    # ═════════════════════════════════════════════════════════════════════
    section("PG6 — verify_required_schema() catches a dropped required table")
    # ═════════════════════════════════════════════════════════════════════
    with db_engine.connect() as conn:
        conn.execute(text("DROP TABLE deleted_library_items CASCADE"))
        conn.commit()
    ok6, missing6 = verify_required_schema()
    check("dropped required table is detected as missing", not ok6)
    check("missing list names the specific table",
          "table:deleted_library_items" in missing6, str(missing6))

finally:
    _teardown_disposable_cluster()
    atexit.unregister(_teardown_disposable_cluster)

print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
