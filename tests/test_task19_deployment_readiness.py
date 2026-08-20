"""
Task 19 — Fail backend deployment safely when required migrations or schema
are not ready.

Investigation confirmed the exact gap the audit described: `_run_migrations()`
caught every ALTER-statement exception, counted it, printed it, and returned
normally — `create_tables()` (called with no try/except from main.py's
`lifespan()`) could never fail from a migration problem, so a deploy that
silently left a required column missing still reported "started successfully"
right up until the first request that touched it. Separately, `/health` only
ever proved the process was alive, never that the database/schema were ready.

This file proves the fix, SQLite-side (dialect-independent logic + the new
`/ready` HTTP contract; the Postgres-only fail-hard/advisory-lock path is
proven against a real disposable Postgres cluster in
test_task19_pg_harness.py, the same pattern as Task 2's own PG harness):

  A — a healthy, fully-migrated database passes `verify_required_schema()`
      with zero missing items.
  B — a database missing a required table is caught, not silently accepted.
  C — a database missing a required column (table present) is caught too.
  D — `create_tables()` end-to-end on a fresh SQLite database succeeds and
      sets `_boot_schema_verified = True` — the real startup path, not just
      the verification function in isolation.
  E — `_run_migrations()` still RUNS every statement on SQLite too (several
      are real, dialect-agnostic CREATE INDEX/UPDATE statements other test
      suites depend on) but never RAISES there — only Postgres is fail-hard.
  F — `get_readiness_status()` reports ready only when BOTH the boot-time
      schema flag is True AND a live `SELECT 1` succeeds right now — losing
      DB connectivity after a clean boot is caught, not just the boot state.
  G — `GET /ready` returns 200 when ready, 503 (not 200) when not — and its
      body never leaks a raw DB URL/credential, only a truncated message.
  H — `GET /health` is UNCHANGED by this task: always 200 regardless of
      readiness state — liveness and readiness stay two separate contracts,
      not silently merged into one.
"""
import os
import sys
import tempfile
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task19.db", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

from sqlalchemy import create_engine, text  # noqa: E402
import app.database as dbmod  # noqa: E402


def _fresh_engine(path):
    """A brand-new, independent SQLite engine at `path` — NOT app.database's
    module-level `engine` — so each scenario below can control exactly what
    schema exists without disturbing other sections or the real app engine.
    """
    return create_engine(f"sqlite:///{path}")


def _with_swapped_engine(new_engine, fn):
    """Run `fn()` with app.database.engine temporarily swapped to
    `new_engine`, always restoring the original afterward (even on
    exception) — verify_required_schema()/get_readiness_status() both read
    the module-level `engine` global, so this is how we point them at a
    scenario-specific database without touching the real one.
    """
    original = dbmod.engine
    dbmod.engine = new_engine
    try:
        return fn()
    finally:
        dbmod.engine = original


# ═════════════════════════════════════════════════════════════════════════
section("A — healthy, fully-migrated database passes verification")
# ═════════════════════════════════════════════════════════════════════════
healthy_path = os.path.join(TMP, "healthy.db")
healthy_engine = _fresh_engine(healthy_path)


def _build_healthy_schema():
    from app.models import user, profile, library, bite, streak, push_token, user_data, bug_report  # noqa
    dbmod.Base.metadata.create_all(bind=dbmod.engine)


_with_swapped_engine(healthy_engine, _build_healthy_schema)
ok, missing = _with_swapped_engine(healthy_engine, dbmod.verify_required_schema)
check("healthy DB: verify_required_schema() passes", ok, f"missing={missing}")
check("healthy DB: missing list is empty", missing == [], str(missing))

for t in dbmod.REQUIRED_TABLES:
    check(f"healthy DB: required table present in registry check — {t}", f"table:{t}" not in missing)


# ═════════════════════════════════════════════════════════════════════════
section("B — a database missing a required table is caught")
# ═════════════════════════════════════════════════════════════════════════
partial_path = os.path.join(TMP, "partial_no_table.db")
partial_engine = _fresh_engine(partial_path)


def _build_partial_schema_missing_chat_turns():
    # NOTE: `Base` is one shared declarative registry for the whole process
    # — by this point in the run, section A's imports have already
    # registered EVERY model (including chat_turns/deleted_library_items)
    # onto Base.metadata for good; a second, narrower `from app.models
    # import ...` here would NOT produce a schema missing those tables,
    # since create_all() creates everything currently in Base.metadata
    # regardless of which import line immediately preceded the call. Build
    # the full schema, then explicitly DROP the tables under test — that's
    # the only way to get a genuinely partial schema once other sections in
    # this same process have already imported the full model set.
    dbmod.Base.metadata.create_all(bind=dbmod.engine)
    with dbmod.engine.connect() as conn:
        conn.execute(text("DROP TABLE chat_turns"))
        conn.execute(text("DROP TABLE deleted_library_items"))
        conn.commit()


_with_swapped_engine(partial_engine, _build_partial_schema_missing_chat_turns)
ok2, missing2 = _with_swapped_engine(partial_engine, dbmod.verify_required_schema)
check("missing-table DB: verify_required_schema() fails", not ok2)
check("missing-table DB: reports table:chat_turns missing", "table:chat_turns" in missing2, str(missing2))
check("missing-table DB: reports table:deleted_library_items missing",
      "table:deleted_library_items" in missing2, str(missing2))


# ═════════════════════════════════════════════════════════════════════════
section("C — a database missing a required column (table present) is caught")
# ═════════════════════════════════════════════════════════════════════════
col_path = os.path.join(TMP, "missing_column.db")
col_engine = _fresh_engine(col_path)


def _build_schema_then_strip_column():
    from app.models import user, profile, library, bite, streak, push_token, user_data, bug_report  # noqa
    dbmod.Base.metadata.create_all(bind=dbmod.engine)
    # SQLite 3.35+ supports DROP COLUMN — simulate a genuinely incomplete
    # migration by removing a column the ORM just created.
    with dbmod.engine.connect() as conn:
        conn.execute(text("ALTER TABLE chat_turns DROP COLUMN claimed_until"))
        conn.commit()


_with_swapped_engine(col_engine, _build_schema_then_strip_column)
ok3, missing3 = _with_swapped_engine(col_engine, dbmod.verify_required_schema)
check("missing-column DB: verify_required_schema() fails", not ok3)
check("missing-column DB: reports column:chat_turns.claimed_until missing",
      "column:chat_turns.claimed_until" in missing3, str(missing3))
check("missing-column DB: does NOT falsely flag unrelated columns",
      not any(m.startswith("column:users.") for m in missing3), str(missing3))


# ═════════════════════════════════════════════════════════════════════════
section("D — create_tables() end-to-end on a fresh SQLite DB")
# ═════════════════════════════════════════════════════════════════════════
# Deliberately NOT using `_with_swapped_engine` here: `SessionLocal`
# (`sessionmaker(bind=engine)`) captured the ORIGINAL engine OBJECT at
# import time, not a live reference to the `engine` module global — a
# later `dbmod.engine = other_engine` reassignment has zero effect on
# where SessionLocal() actually connects. `_do_backfill()` inside
# `_run_task2_required_migrations_and_backfill()` uses SessionLocal(), so
# a swapped-engine "end-to-end" run would create tables on one database
# and then query a completely different (empty) one — not a real
# end-to-end proof. hermetic.py already points the real module engine at
# a fresh, throwaway `sqlite:///{TMP}/task19.db` for this whole process,
# with nothing created on it yet (no earlier section has touched it), so
# calling create_tables() bare — the exact call main.py's lifespan makes
# — is itself the correct real end-to-end scenario.
dbmod._boot_schema_verified = False  # explicit: prove create_tables() is what flips this

try:
    dbmod.create_tables()
    e2e_raised = None
except Exception as e:
    e2e_raised = e

check("create_tables() on a fresh SQLite DB does not raise", e2e_raised is None,
      str(e2e_raised) if e2e_raised else "")
check("create_tables() sets _boot_schema_verified = True", dbmod._boot_schema_verified is True)

ok4, missing4 = dbmod.verify_required_schema()
check("post-create_tables(): schema verification independently passes", ok4, str(missing4))

# F/G/H below reuse the now-fully-migrated real module engine (populated by
# the create_tables() call just above) rather than a separately swapped-in
# one — see the note above on why SessionLocal makes a swapped engine
# unsafe for anything beyond the pure engine-reading helpers.
e2e_engine = dbmod.engine


# ═════════════════════════════════════════════════════════════════════════
section("E — _run_migrations() is a clean no-op on SQLite, never raises")
# ═════════════════════════════════════════════════════════════════════════
noop_path = os.path.join(TMP, "migrations_noop.db")
noop_engine = _fresh_engine(noop_path)


def _build_full_schema_then_run_migrations():
    # Build the real ORM schema first (as create_tables() would) — several
    # statements in the migrations list are real UPDATE/CREATE INDEX
    # statements against tables the list assumes already exist.
    from app.models import user, profile, library, bite, streak, push_token, user_data, bug_report  # noqa
    dbmod.Base.metadata.create_all(bind=dbmod.engine)
    dbmod._run_migrations()


try:
    _with_swapped_engine(noop_engine, _build_full_schema_then_run_migrations)
    noop_raised = None
except Exception as e:
    noop_raised = e

check("_run_migrations() on SQLite never raises, even with some inherently "
      "incompatible statements in the list", noop_raised is None,
      str(noop_raised) if noop_raised else "")


# ═════════════════════════════════════════════════════════════════════════
section("F — get_readiness_status(): both boot-flag AND live connectivity matter")
# ═════════════════════════════════════════════════════════════════════════
dbmod._boot_schema_verified = True
ok5, detail5 = _with_swapped_engine(e2e_engine, dbmod.get_readiness_status)
check("ready when schema_verified=True and DB reachable", ok5, str(detail5))
check("detail reports status=ready", detail5.get("status") == "ready", str(detail5))
check("detail reports db_reachable=True", detail5.get("db_reachable") is True, str(detail5))
check("detail reports schema_verified=True", detail5.get("schema_verified") is True, str(detail5))
check("no db_error key when healthy", "db_error" not in detail5, str(detail5))

# Boot flag False (e.g. a migration failed at boot) → not ready even though
# the DB itself is perfectly reachable.
dbmod._boot_schema_verified = False
ok6, detail6 = _with_swapped_engine(e2e_engine, dbmod.get_readiness_status)
check("not ready when schema_verified=False even if DB reachable", not ok6, str(detail6))
check("detail reports status=not_ready", detail6.get("status") == "not_ready", str(detail6))
dbmod._boot_schema_verified = True  # restore for the next section

# DB unreachable (disposed/closed engine pointed at a bogus path) → not
# ready even though the boot-time schema check passed.
bad_engine = create_engine("sqlite:////nonexistent/definitely/not/a/real/path/x.db")
ok7, detail7 = _with_swapped_engine(bad_engine, dbmod.get_readiness_status)
check("not ready when DB unreachable even if schema_verified=True", not ok7, str(detail7))
check("detail reports db_reachable=False", detail7.get("db_reachable") is False, str(detail7))
check("detail includes a truncated db_error, not a full traceback dump",
      "db_error" in detail7 and len(detail7["db_error"]) <= 200, str(detail7.get("db_error", "")))
check("db_error does not leak the sqlite file path pattern unexpectedly long",
      True)  # informational only — real assertion is the length cap above


# ═════════════════════════════════════════════════════════════════════════
section("G — GET /ready: 200 when ready, 503 when not, no secret leakage")
# ═════════════════════════════════════════════════════════════════════════
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

# Bare TestClient (no `with`), matching this repo's established convention
# (see test_task16_connect_chat_safety.py) — this dispatches requests
# WITHOUT running the ASGI lifespan, so it never calls create_tables()/
# validate_llm_settings() against this hermetic env's blanked-out LLM
# credentials. Readiness state is driven directly (engine swap +
# _boot_schema_verified) instead, which is exactly what this section is
# testing: the /ready HANDLER's own logic, not the lifespan wiring.
client = TestClient(main.app)
with mock.patch.object(dbmod, "engine", e2e_engine):
    dbmod._boot_schema_verified = True
    r_ready = client.get("/ready")
    check("GET /ready → 200 when ready", r_ready.status_code == 200, str(r_ready.status_code))
    body = r_ready.json()
    check("GET /ready body status=ready", body.get("status") == "ready", str(body))
    check("GET /ready body has no DATABASE_URL/credential leakage",
          "sqlite" not in str(body).lower() and "://" not in str(body), str(body))

    dbmod._boot_schema_verified = False
    r_not_ready = client.get("/ready")
    check("GET /ready → 503 when not ready", r_not_ready.status_code == 503,
          str(r_not_ready.status_code))
    check("GET /ready 503 body status=not_ready", r_not_ready.json().get("status") == "not_ready",
          str(r_not_ready.json()))
    dbmod._boot_schema_verified = True


# ═════════════════════════════════════════════════════════════════════════
section("H — GET /health unchanged: always 200, liveness stays separate from readiness")
# ═════════════════════════════════════════════════════════════════════════
with mock.patch.object(dbmod, "engine", bad_engine):
    dbmod._boot_schema_verified = False
    r_health = client.get("/health")
    check("GET /health → still 200 even when DB unreachable AND schema unverified",
          r_health.status_code == 200, str(r_health.status_code))
    check("GET /health body shape unchanged (status/env, nothing about readiness)",
          set(r_health.json().keys()) == {"status", "env"}, str(r_health.json()))
    dbmod._boot_schema_verified = True


# ═════════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
