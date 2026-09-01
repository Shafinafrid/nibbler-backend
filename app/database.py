import logging
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """Create all tables. Called on startup."""
    # Every model module must be listed here. `user_data` and `bug_report` used
    # to be missing: their tables still got created, but ONLY as a side effect of
    # main.py importing the routers (which import the models) before lifespan
    # runs this. A routine import reorder — or lazily importing a router — would
    # have silently stopped creating six tables on any fresh database, failing
    # much later as a 500 on the first /sync call instead of loudly at boot.
    from app.models import (  # noqa
        user, profile, library, bite, streak, push_token, user_data, bug_report, delivery,
        personalization,
    )
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    _run_task2_required_migrations_and_backfill()
    # Task 19 (Aug 2026): deterministic readiness gate. A migration statement
    # not raising is not proof the effective schema is correct — it only
    # proves that one SQL call succeeded. This re-derives the real schema
    # from the database itself (information_schema/pg_catalog on Postgres,
    # PRAGMA/sqlite_master on SQLite) and refuses to finish startup if
    # anything this app actually relies on is missing, instead of letting a
    # partially-migrated database serve traffic that looks healthy on
    # `/health` (liveness) right up until the first request that touches the
    # missing column/constraint 500s. See `get_readiness_status()` / `/ready`
    # in main.py for the corresponding per-request contract.
    ok, missing = verify_required_schema()
    global _boot_schema_verified
    if not ok:
        _boot_schema_verified = False
        raise RuntimeError(
            "[readiness] required schema verification FAILED — refusing to finish "
            f"startup. Missing: {', '.join(missing)}. See nibbler-backend/CLAUDE.md, "
            "'Deployment readiness (Task 19)'."
        )
    _boot_schema_verified = True
    print(f"[readiness] schema verification passed — {len(REQUIRED_TABLES)} table(s), "
          f"{len(REQUIRED_COLUMNS)} column(s), "
          f"{len(REQUIRED_PG_CONSTRAINTS) if engine.dialect.name == 'postgresql' else 0} "
          "constraint(s)/index(es) checked")


# Arbitrary but stable Postgres advisory-lock key, scoped to this one
# migration+backfill sequence. Session-level (pg_advisory_lock/unlock, not
# the xact variant) and held on ONE connection for the whole block below.
TASK2_ADVISORY_LOCK_KEY = 918_273_645

# Task 19: a second, distinct advisory-lock key for `_run_migrations()`'s
# (pre-Task-2, general) statement list — kept separate from
# TASK2_ADVISORY_LOCK_KEY so the two sequences don't contend with each other
# for no reason; both still run strictly in sequence within one boot either
# way (create_tables() calls them one after another), this only matters for
# two DIFFERENT processes racing the SAME sequence during a rolling deploy.
MIGRATIONS_ADVISORY_LOCK_KEY = 918_273_646

# Set True only once `create_tables()` has run `verify_required_schema()`
# and it passed. `get_readiness_status()` (used by GET /ready) reads this —
# never re-derived per request, since the schema doesn't change at runtime
# and information_schema/pg_catalog queries aren't free. Starts False so a
# request arriving before startup finishes (shouldn't happen — lifespan
# awaits create_tables() before the app accepts traffic — but a defensive
# default matters more than an optimistic one here) reports not-ready
# rather than ready.
_boot_schema_verified = False


def _ensure_mixed_version_fencing(conn) -> None:
    """Task 2 lifecycle remediation, consolidated backend pass — mixed-
    version cutover fencing, Option B (durable queue + autonomous Python
    worker, per the architecture decision: complex Free-capacity business
    logic — capacity checks, promotion to consumed/premium/grandfathered —
    does not belong duplicated across two database trigger languages).

    `reconcile_unaccounted_processed_items()` is a STARTUP-ONLY sweep; an
    old-code worker (mid-flight when a new backend started) can finish
    AFTER that sweep already ran and do exactly what old code always did:
    `item.processed = True; db.commit()`, with no `entitlement_status`.
    Nothing then revisits that row until the NEXT boot's sweep — it would
    stay `processed=True` (accessible, looks done) and unaccounted
    indefinitely.

    A database trigger is the only mechanism that reacts to that write
    regardless of who performs it or when — including the exact write
    pattern this repo's own test scripts use to prove it
    (`LibraryItem.__table__.update()`/`Query.update()`, which bypasses
    SQLAlchemy ORM-level Python event hooks entirely, so an
    `@event.listens_for` approach would NOT catch it). The trigger, in ONE
    atomic transaction with the old-writer's own write:
      1. fences the row IMMEDIATELY and INACCESSIBLY — sets
         `entitlement_status = 'released'` AND `processed = False` (not
         merely the status). Clearing `processed` too (not just the
         earlier design's status-only fence) is required so an old,
         processed-only reader — one that predates `entitlement_status`
         entirely and filters ONLY on `processed = True` — genuinely
         cannot find the row; leaving `processed = True` would satisfy
         that old reader's exact query regardless of what
         `entitlement_status` says.
      2. stamps a fresh `reconciliation_generation` on the item and
         inserts/upserts exactly one row into `reconciliation_tasks`
         naming that same generation — the durable, autonomously-
         processed reconciliation work `retry_reconciliation_tasks`
         (entitlement_service.py) discovers and resolves on the
         production scheduler's own recurring tick, never only at the
         next process restart.

    Deliberately UPDATE-only, never INSERT: both the real hazard (a row
    created at upload time with `processed=False`, later flipped to True
    by an old-code background task) and every test reproduction of it are
    an UPDATE on an EXISTING row — nothing legitimate INSERTS a row with
    `processed=True` already set. Restricting to UPDATE was a deliberate
    correction after this trigger, first written to also fire on INSERT,
    fenced ordinary test/ORM fixtures that construct an already-processed
    row in one step for unrelated reasons (several pre-existing Task 2
    test suites do exactly this) — those were never the mixed-version
    hazard this exists to catch.

    Postgres: a BEFORE UPDATE trigger mutates NEW directly (no extra
    statement for the fence itself) and performs one additional INSERT ...
    ON CONFLICT for the queue row, inside the same trigger invocation —
    still one atomic transaction with the triggering UPDATE.
    SQLite: triggers cannot mutate NEW in place on a real table, so an
    AFTER UPDATE trigger issues a corrective UPDATE, then reads the
    generation it JUST wrote back via a SELECT (statements within one
    trigger firing execute sequentially, each seeing prior statements'
    effects) to insert/upsert the matching queue row. The corrective
    UPDATE's own guard (`entitlement_status IS NULL`) is already false on
    that write itself, so it terminates in one extra pass even if
    `recursive_triggers` happens to be enabled.

    Also guards on `OLD.processed IS NOT TRUE` (i.e. only the exact
    moment `processed` transitions INTO true) — not merely "is this row
    currently processed with a null status". Without that, EVERY later,
    otherwise-unrelated UPDATE to an already-processed row sharing this
    shape (e.g. `is_active` being toggled during a downgrade reconcile)
    would re-fire the fencing condition and fence a row nothing was ever
    trying to hide — found via a genuine pre-existing-suite regression
    where a `finalize_lock_selection` bulk `is_active` update on an
    unrelated locked row triggered fencing.

    Idempotent both ways: `CREATE OR REPLACE FUNCTION` + `DROP TRIGGER IF
    EXISTS`/`CREATE TRIGGER` (Postgres, always replaces) and `DROP
    TRIGGER IF EXISTS` + `CREATE TRIGGER` (SQLite — corrected from an
    earlier `CREATE TRIGGER IF NOT EXISTS`, which silently preserved a
    stale installed definition under a name collision instead of ever
    replacing it) — safe to call on every boot, including against a
    database that already has it installed, and now guaranteed to pick up
    a changed definition rather than freezing the first one ever
    installed.

    Takes an ALREADY-OPEN connection rather than an engine, and (for
    Postgres) MUST be called from inside the same advisory-lock-holding
    connection `_run_task2_required_migrations_and_backfill` already uses
    for the required-column migrations. `pg_advisory_lock` is a SESSION-
    scoped lock tied to one specific connection — opening a fresh
    connection here would perform this DDL entirely outside that lock's
    protection. Found via the checked-in PG harness's real two-connection
    concurrent-boot scenario, which reproduced Postgres's own "tuple
    concurrently updated" catalog error when two sessions ran `CREATE OR
    REPLACE FUNCTION` for the identical function at the same moment — the
    exact class of race the advisory lock exists to prevent for every
    other statement in this sequence; this one was missing it."""
    from sqlalchemy import text

    dialect = conn.dialect.name

    if dialect == "postgresql":
        conn.execute(text(
            """
            CREATE OR REPLACE FUNCTION fence_unaccounted_processed_items() RETURNS trigger AS $$
            DECLARE
                gen_id VARCHAR;
            BEGIN
                IF (OLD.processed IS NOT TRUE) AND NEW.processed IS TRUE
                   AND NEW.entitlement_status IS NULL THEN
                    gen_id := gen_random_uuid()::text;
                    NEW.entitlement_status := 'released';
                    NEW.processed := FALSE;
                    NEW.reconciliation_generation := gen_id;
                    INSERT INTO reconciliation_tasks
                        (id, item_id, generation, state, retry_count, created_at, updated_at)
                    VALUES
                        (gen_random_uuid()::text, NEW.id, gen_id, 'pending', 0, now(), now())
                    ON CONFLICT (item_id) DO UPDATE SET
                        generation = EXCLUDED.generation,
                        state = 'pending',
                        claimed_by = NULL,
                        claimed_until = NULL,
                        updated_at = now();
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        ))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_fence_unaccounted_processed_items ON library_items"))
        conn.execute(text(
            """
            CREATE TRIGGER trg_fence_unaccounted_processed_items
            BEFORE UPDATE ON library_items
            FOR EACH ROW EXECUTE FUNCTION fence_unaccounted_processed_items()
            """
        ))
    else:
        # SQLite (local dev + every test script in this repo) — no
        # multi-worker deploy race to guard against, so no lock needed.
        # DROP + CREATE (not `IF NOT EXISTS`) so a changed definition is
        # always picked up, never silently frozen on the first install.
        conn.execute(text("DROP TRIGGER IF EXISTS trg_fence_unaccounted_processed_items_upd"))
        conn.execute(text("""
            CREATE TRIGGER trg_fence_unaccounted_processed_items_upd
            AFTER UPDATE ON library_items
            WHEN (OLD.processed IS NOT 1) AND NEW.processed = 1 AND NEW.entitlement_status IS NULL
            BEGIN
                UPDATE library_items
                    SET entitlement_status = 'released',
                        processed = 0,
                        reconciliation_generation = lower(hex(randomblob(16)))
                    WHERE id = NEW.id AND entitlement_status IS NULL;
                INSERT INTO reconciliation_tasks (id, item_id, generation, state, retry_count, created_at, updated_at)
                    SELECT lower(hex(randomblob(16))), NEW.id, reconciliation_generation, 'pending', 0,
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    FROM library_items WHERE id = NEW.id
                    ON CONFLICT(item_id) DO UPDATE SET
                        generation = excluded.generation,
                        state = 'pending',
                        claimed_by = NULL,
                        claimed_until = NULL,
                        updated_at = CURRENT_TIMESTAMP;
            END
        """))
        conn.commit()


def _run_task2_required_migrations_and_backfill():
    """Task 2 (Aug 2026, remediated after Hermes's audit): schema + backfill
    that MUST succeed for the app to be safe to serve traffic — unlike the
    lenient, best-effort statements in `_run_migrations()` above, a failure
    here is a BOOT FAILURE (see `lifespan()` in main.py, which calls
    `create_tables()` with no try/except — an exception here stops the app
    from becoming ready), not a printed warning. Fail-open here would mean
    the entitlement cap silently stops being enforced for every account.

    Postgres only: SQLite (local/test) already gets every Task 2 column,
    with the correct nullability and defaults, straight from the ORM via
    `Base.metadata.create_all()` above — there is no ALTER TABLE syntax to
    run (SQLite doesn't support `ADD COLUMN IF NOT EXISTS`, and never has —
    see the ~46 pre-existing statements in `_run_migrations()`, which is why
    that loop is lenient), and no multi-worker deploy race to guard with an
    advisory lock in a single-process test run.
    """
    from app.services.entitlement_service import (
        backfill_existing_accounts, reconcile_unaccounted_processed_items,
        retry_cleanup_tasks, retry_reconciliation_tasks,
    )

    def _do_backfill():
        db = SessionLocal()
        try:
            ok, failed = backfill_existing_accounts(db)
            print(f"[entitlement-backfill] reconciled {ok} account(s), {failed} failed")
            if failed:
                raise RuntimeError(
                    f"entitlement backfill failed for {failed} account(s) — see the "
                    "logged exception(s) above for the per-account cause"
                )
            # Mixed-version cutover reconciliation (Task 2, 3rd-audit
            # remediation #10) — runs on EVERY boot, not just once: an
            # old-code worker still finishing after this boot's cutover can
            # leave a NEW `processed=True, entitlement_status IS NULL` row
            # at any time, so this can't be a one-shot migration step the
            # way account backfill is. Idempotent — see its docstring. The
            # database-level fencing trigger (installed below/above,
            # dialect-dispatched) is the CONTINUOUS safety net between boots;
            # this sweep is what actually promotes a fenced/legacy row into
            # real accounting.
            fixed, recon_failed = reconcile_unaccounted_processed_items(db)
            if fixed or recon_failed:
                print(f"[entitlement-reconcile] repaired {fixed} unaccounted item(s), {recon_failed} failed")
            if recon_failed:
                raise RuntimeError(
                    f"unaccounted-item reconciliation failed for {recon_failed} item(s) — see the "
                    "logged exception(s) above for the per-item cause"
                )
            # Autonomous durable-cleanup retry (Task 2 lifecycle remediation,
            # Follow-up 2A) — real startup invocation of the same scan/
            # runner an operator or a future scheduled job would call; see
            # its docstring in entitlement_service.py for claim/idempotency
            # behavior. A boot-time failure here is logged, not fatal — an
            # unretried cleanup record stays durably retryable on the next
            # boot (or the next explicit call), unlike the accounting passes
            # above, which must succeed for the app to be safe to serve.
            try:
                cleaned, cleanup_failed = retry_cleanup_tasks(db)
                if cleaned or cleanup_failed:
                    print(f"[cleanup-retry] resolved {cleaned} durable cleanup task(s), {cleanup_failed} failed")
            except Exception:
                logging.getLogger(__name__).exception("startup cleanup-task retry failed")
            # Mixed-version cutover reconciliation queue (Task 2
            # consolidated backend pass, Option B) — same non-fatal,
            # startup-convenience invocation as cleanup retry above; the
            # scheduler (notification_service.start_scheduler) is the
            # CONTINUOUS autonomous path that makes this NOT restart-only,
            # this is just immediate effect on the boot that installed the
            # trigger extension.
            try:
                recon_resolved, recon_task_failed = retry_reconciliation_tasks(db)
                if recon_resolved or recon_task_failed:
                    print(f"[reconciliation-retry] resolved {recon_resolved} durable reconciliation "
                          f"task(s), {recon_task_failed} failed")
            except Exception:
                logging.getLogger(__name__).exception("startup reconciliation-task retry failed")
        finally:
            db.close()

    if engine.dialect.name != "postgresql":
        # SQLite: no multi-worker deploy race to guard against.
        with engine.connect() as conn:
            _ensure_mixed_version_fencing(conn)
        _do_backfill()
        return

    from sqlalchemy import text

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # Blocking (not pg_try_advisory_lock): serializes this whole
        # required-schema + backfill sequence across every concurrently-
        # booting Railway worker (a rolling deploy briefly runs old+new
        # instances together). Whichever instance gets here first does the
        # real work; every other instance blocks, then finds the schema
        # already applied and every account already reconciled
        # (free_lock_state_token IS NOT NULL) and returns almost
        # immediately — never two instances racing the same ALTER or the
        # same account's backfill.
        #
        # `_ensure_mixed_version_fencing`'s `CREATE OR REPLACE FUNCTION`/
        # `CREATE TRIGGER` DDL MUST also run under this SAME lock — found
        # via the checked-in PG harness's real two-connection concurrent-
        # boot scenario (PG7), which reproduced Postgres's own
        # "tuple concurrently updated" catalog error when two sessions ran
        # `CREATE OR REPLACE FUNCTION` for the identical function at the
        # same moment. DDL against shared catalog objects needs the exact
        # same serialization the required-column migrations already get;
        # it was a genuine bug to have left it outside this block.
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": TASK2_ADVISORY_LOCK_KEY})
        try:
            applied = 0
            for sql in TASK2_REQUIRED_MIGRATIONS:
                conn.execute(text(sql))
                applied += 1
            print(f"[migration] Task 2 required: {applied} applied/verified")
            _ensure_mixed_version_fencing(conn)
            _do_backfill()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TASK2_ADVISORY_LOCK_KEY})


# Required Task 2 schema — additive columns plus DATABASE-LEVEL NOT NULL/
# DEFAULT constraints matching the ORM (`nullable=False` in the models was
# previously enforced only in Python; a raw SQL write or a future bug could
# still leave a NULL in Postgres). The two-step ADD COLUMN…DEFAULT then
# UPDATE…WHERE IS NULL then ALTER…SET NOT NULL order is deliberate: Postgres
# 11+ backfills the DEFAULT onto every existing row as part of ADD COLUMN
# without a table rewrite, but the explicit UPDATE covers any row written
# between two separate ALTER statements on a slow/partial deploy, so SET NOT
# NULL can never fail on a genuinely-NULL row.
TASK2_REQUIRED_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS successful_sources_total INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_lock_state_token VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS is_unlocked_selection BOOLEAN DEFAULT FALSE",
    # Reservation primitive + selection provenance (remediation)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS reserved_sources_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_lock_last_effective_premium BOOLEAN",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS entitlement_status VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMP",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS selection_kind VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP",
    # Renewable reservation lease (remediation #3) — nullable, no backfill
    # needed: every pre-existing row is either not 'pending' (nothing to
    # lease) or, in the rare case it is, gets reaped on first touch since a
    # NULL expiry is treated as "not currently leased" by _reap_stale_reservations.
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS reservation_lease_token VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS reservation_lease_expires_at TIMESTAMP",
    # Attempt-scoped ownership + durable cleanup marker (3rd-audit remediation)
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS last_processing_attempt_id VARCHAR",
    # Mixed-version cutover fencing generation (consolidated backend pass) —
    # must exist before _ensure_mixed_version_fencing's trigger DDL, which
    # writes to it, ever runs against an upgraded (not fresh-created)
    # Postgres database.
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS reconciliation_generation VARCHAR",
    # Worker-attempt admission (Task 2 final consolidated backend pass) —
    # separate from reservation_lease_token; see the model's own docstring.
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS worker_attempt_id VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS worker_attempt_expires_at TIMESTAMP",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS cleanup_state VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS cleanup_detail JSON",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS deletion_state VARCHAR",
    "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS deletion_detail JSON",
    "UPDATE users SET successful_sources_total = 0 WHERE successful_sources_total IS NULL",
    "UPDATE users SET reserved_sources_count = 0 WHERE reserved_sources_count IS NULL",
    "UPDATE library_items SET is_unlocked_selection = FALSE WHERE is_unlocked_selection IS NULL",
    "ALTER TABLE users ALTER COLUMN successful_sources_total SET DEFAULT 0",
    "ALTER TABLE users ALTER COLUMN successful_sources_total SET NOT NULL",
    "ALTER TABLE users ALTER COLUMN reserved_sources_count SET DEFAULT 0",
    "ALTER TABLE users ALTER COLUMN reserved_sources_count SET NOT NULL",
    "ALTER TABLE library_items ALTER COLUMN is_unlocked_selection SET DEFAULT FALSE",
    "ALTER TABLE library_items ALTER COLUMN is_unlocked_selection SET NOT NULL",
    # Task 2 closeout (Verified Blocker 4): cleanup_tasks identity now
    # includes artifact_key, so multiple image keys can each get their own
    # durable row for the same attempt. Every existing NULL is normalized
    # to "" FIRST — Postgres unique constraints treat NULL as distinct, so
    # a stray NULL would otherwise let a duplicate slip past this exact
    # constraint the same way the OLD 3-column constraint never noticed a
    # second image at all.
    "UPDATE cleanup_tasks SET artifact_key = '' WHERE artifact_key IS NULL",
    "ALTER TABLE cleanup_tasks ALTER COLUMN artifact_key SET DEFAULT ''",
    "ALTER TABLE cleanup_tasks ALTER COLUMN artifact_key SET NOT NULL",
    "ALTER TABLE cleanup_tasks DROP CONSTRAINT IF EXISTS uq_cleanup_task_identity",
    "ALTER TABLE cleanup_tasks DROP CONSTRAINT IF EXISTS uq_cleanup_task_identity_v2",
    "ALTER TABLE cleanup_tasks ADD CONSTRAINT uq_cleanup_task_identity_v2 "
    "UNIQUE (item_id, attempt_token, artifact_kind, artifact_key)",
]


def _run_migrations():
    """
    Column-level migrations — adds missing columns/indexes without touching
    existing data. Add new ALTER statements here whenever the models gain
    new columns so Railway auto-applies them on next deploy.

    Task 19 (Aug 2026): every statement in this list backs a column, index,
    or data-backfill that live code already reads or writes — there is no
    "cosmetic" entry here (an unused column would just be deleted, not left
    in this list). Before Task 19 a failed statement was only logged, never
    raised, so a deploy could boot "successfully" onto a database silently
    missing something the app relies on and fail hours later on whichever
    endpoint touches it first — a false-green deployment.

    **This is fail-closed on Postgres only.** Every statement still RUNS on
    every dialect (unchanged from before Task 19) — only what happens AFTER
    a failure differs:
      - **Postgres (production):** the whole sequence runs behind a
        session-level advisory lock (MIGRATIONS_ADVISORY_LOCK_KEY, distinct
        from Task 2's own key, so the two sequences don't contend for no
        reason) so a rolling deploy's overlapping old+new workers can't
        race the same ALTER — see TASK2_ADVISORY_LOCK_KEY's docstring above
        for why that race is real, not theoretical (a reproduced Postgres
        catalog error, checked in at tests/test_task2_pg_harness.py). A
        failed statement does not abort the connection for the rest
        (AUTOCOMMIT — matches the Task 2 block): every statement still gets
        a chance to run, and every failure is reported together, before the
        whole sequence raises and stops startup (see `create_tables()`'s
        caller in main.py's `lifespan()`, which has no try/except around
        it).
      - **Every other dialect (SQLite — local/test):** stays lenient, on
        purpose, not merely un-updated. A large share of these statements
        are Postgres-only syntax that `Base.metadata.create_all()` already
        made unnecessary on SQLite — `ALTER TABLE ... ADD COLUMN IF NOT
        EXISTS` isn't valid SQLite at all (no IF NOT EXISTS there), and
        `DROP CONSTRAINT` doesn't exist in SQLite full stop — so those
        entries are EXPECTED to fail here and that failure carries no
        signal about the real schema. But NOT every statement is like
        that: several `CREATE [UNIQUE] INDEX IF NOT EXISTS ...` and
        `UPDATE ...` backfill statements in this list ARE valid, dialect-
        agnostic SQL that several existing test suites depend on actually
        running against their SQLite fixtures (confirmed the hard way —
        an earlier version of this Task 19 change made SQLite skip the
        whole list outright and broke test_backend_primary.py,
        test_batch_b.py and test_task8_entitlement_unification.py, all of
        which assert on real side effects of specific statements in this
        exact list). So SQLite still executes everything, exactly as
        before, and simply never raises on a failure — the per-statement
        try/except below is unconditional; only the raise at the very
        bottom is dialect-gated.
    """
    from sqlalchemy import text

    is_postgres = engine.dialect.name == "postgresql"

    migrations = [
        # library_items — embedding pipeline (May 2026)
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS chunk_count INTEGER DEFAULT 0",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS processing_error VARCHAR",
        # library_items — nibble sessions (July 2026)
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS mode VARCHAR DEFAULT 'wisdom'",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS kind VARCHAR DEFAULT 'book'",
        # library_items — pictures pulled out of the book at upload (July 2026)
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS images JSON",
        # library_items — scanned-PDF OCR (Aug 2026)
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS ocr_status VARCHAR",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS ocr_pages_done INTEGER DEFAULT 0",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS ocr_pages_total INTEGER DEFAULT 0",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS author VARCHAR",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS growth_profile_name VARCHAR",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS story_progress INTEGER DEFAULT 0",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS source_url VARCHAR",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS file_size INTEGER",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now()",
        # library_items — active nibble sources (July 2026)
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        # personalization_questions.claimed_until (audit fix, Aug 2026) — the
        # answer endpoint's claim lease. The table itself was created by
        # create_all on first deploy, but this column was added afterwards,
        # so an existing production table needs the ALTER.
        "ALTER TABLE personalization_questions ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMP",
        "ALTER TABLE personalization_questions ADD COLUMN IF NOT EXISTS claimed_by VARCHAR",
        # Did the ORIGINAL file reach S3? `processed` never meant that.
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS archive_status VARCHAR",
        # daily_bites — per-book card-deck sessions (July 2026)
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS library_item_id VARCHAR",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS cards JSON",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS quiz JSON",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS read_length INTEGER",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS mode VARCHAR",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS chapter VARCHAR",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS headline VARCHAR",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS preview TEXT",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS goal_passage TEXT",
        # daily_bites — session lifecycle: scheduled generation + hold-until-read (July 2026)
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS origin VARCHAR DEFAULT 'manual'",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS read_at TIMESTAMP",
        # daily_bites — generation claim/lease (finding #5, Aug 2026): protects
        # the expensive LLM call itself, not just the stored row, from a
        # concurrent double-tap/retry racing generate_session_for_item. See
        # the model's docstring in app/models/bite.py.
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS claimed_by VARCHAR",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMP",
        # users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR",
        # profiles — local-first growth state sync (July 2026)
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS growth_state JSON",
        # profiles — deletion tombstones (finding #7, Sep 2026): a never-
        # shrinking union of profile ids any device has recorded as deleted,
        # enforced on every PUT /profile/growth independent of the whole-blob
        # LWW outcome, so a stale device can't resurrect a deleted profile by
        # pushing a later timestamp. See app/models/profile.py.
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS deleted_profile_ids JSON",
        # push_tokens — minute-precision delivery (July 2026)
        "ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS notification_minute INTEGER DEFAULT 0",
        "ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS streak_alerts_enabled BOOLEAN DEFAULT TRUE",
        # push_tokens — truthful/recoverable/timezone-safe settings (Task 4, Aug 2026)
        "ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS notification_local_hour INTEGER",
        "ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS notification_local_minute INTEGER",
        "ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE streaks ADD COLUMN IF NOT EXISTS last_completed_at TIMESTAMP",
        "ALTER TABLE daily_bites ADD COLUMN IF NOT EXISTS chunk_ids JSON",
        # users — identity + device context (July 2026, full-account sync)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS locale VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS platform VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS app_version VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
        # Task 7 (Aug 2026): device context + trial-anchor for the account-
        # deletion abuse guard. email_account_history is a brand-new table,
        # picked up automatically by create_all() — no ALTER needed for it.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS device_model VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS os_version VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_anchor_at TIMESTAMP",
        # Task 7: account_erasures already existed (Task2 Closeout) — these
        # ARE new columns on an existing table, so (unlike email_account_history,
        # which is brand-new and picked up by create_all()) they need entries here.
        "ALTER TABLE account_erasures ADD COLUMN IF NOT EXISTS sheet_row INTEGER",
        "ALTER TABLE account_erasures ADD COLUMN IF NOT EXISTS snapshot_sheet_row INTEGER",
        "ALTER TABLE account_erasures ADD COLUMN IF NOT EXISTS deletion_started_at TIMESTAMP",
        "ALTER TABLE account_erasures ADD COLUMN IF NOT EXISTS deletion_completed_at TIMESTAMP",
        "ALTER TABLE account_erasures ADD COLUMN IF NOT EXISTS personal_data_redacted_at TIMESTAMP",
        # Task 8 (Aug 2026): canonical entitlement provenance + webhook idempotency.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS entitlement_source VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_held_paid_entitlement BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_synced_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_webhook_event_id VARCHAR",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_webhook_event_at_ms BIGINT",
        # One-time data backfill (audit finding, Aug 2026): every existing
        # is_premium=True row predates entitlement_source's existence — it
        # was set by hand (there was never any code path that wrote it
        # before this task), which is EXACTLY the pre-existing "manual comp"
        # meaning _plan_label already encodes ("premium (comp)" for any
        # is_premium=True user, unconditionally). Without this, the new
        # resolver's back-compat branch had no way to distinguish that from
        # a paid grant and misreported it as 'paid'. Safe to re-run every
        # boot — a no-op once entitlement_source is populated.
        "UPDATE users SET entitlement_source = 'complimentary' "
        "WHERE is_premium = TRUE AND entitlement_source IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username ON users (username) WHERE username IS NOT NULL",
        # ── notes / highlights identity (rewritten 2026-07-26) ────────────────
        # See the block comment in app/models/user_data.py. The old key
        # (user_id, book_id, card_index) collided across daily sessions, because
        # card_index is a position within ONE deck and every deck restarts at 0.
        #
        # ORDER MATTERS HERE: add the column, drop the old unconditional key,
        # THEN create the two partial ones. Leaving the old key in place would
        # keep enforcing the collision no matter what the new indexes say.
        "ALTER TABLE notes ADD COLUMN IF NOT EXISTS daily_bite_id VARCHAR",
        "ALTER TABLE highlights ADD COLUMN IF NOT EXISTS daily_bite_id VARCHAR",
        # create_all() originally declared these as CONSTRAINTS (via
        # UniqueConstraint), which a DROP INDEX cannot remove — but on a
        # database built after that declaration was removed they may exist as
        # plain indexes instead. Both forms are attempted; each is a harmless
        # no-op when the other applies.
        "ALTER TABLE notes DROP CONSTRAINT IF EXISTS uq_notes_user_book_card",
        "ALTER TABLE highlights DROP CONSTRAINT IF EXISTS uq_highlights_user_book_card",
        "DROP INDEX IF EXISTS uq_notes_user_book_card",
        "DROP INDEX IF EXISTS uq_highlights_user_book_card",
        # Rows written from 2026-07-26 on: scoped to the session they belong to.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notes_user_bite_card "
        "ON notes (user_id, daily_bite_id, card_index) WHERE daily_bite_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_highlights_user_bite_card "
        "ON highlights (user_id, daily_bite_id, card_index) WHERE daily_bite_id IS NOT NULL",
        # Rows written before it: cannot be attributed to a session after the
        # fact, so they keep the old key and are left exactly as they are.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notes_user_book_card_legacy "
        "ON notes (user_id, book_id, card_index) WHERE daily_bite_id IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_highlights_user_book_card_legacy "
        "ON highlights (user_id, book_id, card_index) WHERE daily_bite_id IS NULL",
        # chat / completions — read paths are always scoped to one user+book
        "CREATE INDEX IF NOT EXISTS ix_chat_messages_user_book ON chat_messages (user_id, book_id)",
        "CREATE INDEX IF NOT EXISTS ix_completions_user_book ON completions (user_id, book_id)",
        # chat_context_chunks — every /connect/chat call reads the full
        # accumulated set for (user_id, book_id) before retrieval; the
        # UniqueConstraint on the model already gives Postgres an index
        # covering (user_id, book_id, chunk_index), but that composite index
        # is only efficient when queries also filter chunk_index — the read
        # path here never does, so a plain (user_id, book_id) index is what
        # actually serves it.
        "CREATE INDEX IF NOT EXISTS ix_chat_context_chunks_user_book "
        "ON chat_context_chunks (user_id, book_id)",
        # daily_bites / saved_bites — these were applied to PRODUCTION BY HAND on
        # 2026-07-16 and never declared anywhere in code, so a fresh database
        # (local dev, a restore, a second environment) silently didn't get them —
        # and the IntegrityError handling that depends on them
        # (session_service.generate_session_for_item, POST /bites/{id}/save)
        # became dead code that could never fire. Re-running them here is a
        # no-op on prod, where they already exist.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_bites_user_item_date "
        "ON daily_bites (user_id, library_item_id, date) WHERE library_item_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_bites_user_bite ON saved_bites (user_id, bite_id)",
        # user_settings — 3-way appearance (July 2026): light / dark / black.
        # `dark_mode` (bool) stays as-is for older clients still writing it;
        # `theme_mode` is the new source of truth once a client has sent it.
        "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS theme_mode VARCHAR",
        # streaks — Task 20 (Aug 2026): per-day idempotency/catch-up marker
        # for the streak alert, see notification_service._notify_streak_alert_slot.
        "ALTER TABLE streaks ADD COLUMN IF NOT EXISTS last_alert_sent_date DATE",
        # users — Task 3 fix (Aug 2026): sticky-until-read featured-nibble
        # pointer, see notification_service._feature_bite().
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_featured_bite_id VARCHAR",
        # users — Task 8 fix (Aug 2026): per-source expiry, see
        # User.recompute_premium_until(). Backfill seeds each existing row's
        # ALREADY-CORRECT current premium_until into whichever source
        # entitlement_source names, so a deploy doesn't drop a live grant —
        # after this, premium_until is derived and no write path sets it
        # directly again. Safe to re-run: only fires while the new columns
        # are still both NULL for that row.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS paid_premium_until TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS complimentary_until TIMESTAMP",
        "UPDATE users SET paid_premium_until = premium_until "
        "WHERE entitlement_source = 'paid' AND premium_until IS NOT NULL "
        "AND paid_premium_until IS NULL AND complimentary_until IS NULL",
        "UPDATE users SET complimentary_until = premium_until "
        "WHERE entitlement_source = 'complimentary' AND premium_until IS NOT NULL "
        "AND paid_premium_until IS NULL AND complimentary_until IS NULL",
        # Task 2's own schema (successful_sources_total, free_lock_state_token,
        # is_unlocked_selection, the reservation/provenance columns, and their
        # backfill) moved to TASK2_REQUIRED_MIGRATIONS below — kept on its own
        # advisory lock + backfill sequence rather than merged into this list
        # (both are fail-closed on Postgres as of Task 19; TASK2's block also
        # runs a Python data backfill this one doesn't). The original backfill
        # here (a raw COUNT(*) of every processed row) was ALSO semantically
        # wrong after the entitlement-accounting remediation: it counted
        # Premium-created successes against the Free counter too, which is
        # exactly the bug that remediation fixes — removed rather than left
        # to race the correct Python backfill.
    ]

    applied, failures = 0, []
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # pg_advisory_lock is a Postgres builtin — calling it on SQLite would
        # itself be a hard error, so the lock is Postgres-only, same as the
        # raise-on-failure behavior below (both branches still run every
        # statement in `migrations` either way).
        if is_postgres:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATIONS_ADVISORY_LOCK_KEY})
        try:
            for sql in migrations:
                try:
                    conn.execute(text(sql))
                    applied += 1
                except Exception as e:
                    failures.append((sql, str(e)))
                    print(f"[migration] FAILED: {sql[:70]}… → {e}")
        finally:
            if is_postgres:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATIONS_ADVISORY_LOCK_KEY})

    print(f"[migration] done: {applied} applied/verified, {len(failures)} failed")
    if failures and is_postgres:
        summary = "; ".join(f"{sql[:70]}… → {err[:120]}" for sql, err in failures)
        raise RuntimeError(
            f"[migration] {len(failures)} required migration statement(s) failed — "
            f"refusing to finish startup: {summary}"
        )


# ═════════════════════════════════════════════════════════════════════════
# Task 19 — deterministic schema verification + the /ready contract
# ═════════════════════════════════════════════════════════════════════════
# A short, deliberately non-exhaustive list: not "every column this app
# has", but the constructs whose ABSENCE would silently break integrity
# guarantees the code assumes hold — idempotency keys, dedupe constraints,
# the tables backing this session's own audit fixes (chat_turns,
# deleted_library_items) — the exact kind of gap a migration that "ran
# without error" doesn't actually prove closed. Extend this list, don't
# replace it, when a future task adds another constraint of this kind.
REQUIRED_TABLES = [
    "users", "library_items", "daily_bites", "chat_turns",
    "deleted_library_items", "account_erasures", "cleanup_tasks",
    "delivery_cycles", "chat_context_chunks", "personalization_questions",
]
REQUIRED_COLUMNS = [
    ("chat_turns", "turn_id"), ("chat_turns", "status"),
    ("chat_turns", "claimed_by"), ("chat_turns", "claimed_until"),
    ("deleted_library_items", "user_id"), ("deleted_library_items", "deleted_at"),
    ("library_items", "entitlement_status"), ("library_items", "reservation_lease_token"),
    ("library_items", "is_active"),
    ("users", "reserved_sources_count"), ("users", "entitlement_source"),
    ("daily_bites", "origin"), ("daily_bites", "read_at"),
    # Finding #5 (Aug 2026) — the generation claim/lease. Missing either
    # column would mean the claim silently can't be taken/verified,
    # collapsing straight back to the double-LLM-call bug this fix closes.
    ("daily_bites", "claimed_by"), ("daily_bites", "claimed_until"),
    # Task 20 — the durable delivery-cycle ledger itself must be verified
    # present/correct at boot, per Task 20's own requirement #11 ("the
    # backend must not report ready if its required durable delivery
    # schema cannot be used") — see app/services/delivery_lifecycle.py.
    ("delivery_cycles", "user_id"), ("delivery_cycles", "cycle_date"),
    ("delivery_cycles", "state"), ("delivery_cycles", "claimed_by"),
    ("delivery_cycles", "claimed_until"), ("delivery_cycles", "due_at"),
    # Task 8 fix — without these two columns the webhook/sync-premium write
    # paths would silently fall back to corrupting the shared premium_until
    # again, exactly the bug this migration exists to close.
    ("users", "paid_premium_until"), ("users", "complimentary_until"),
    # Finding #7 (Sep 2026) — deletion tombstones. Missing this column would
    # mean the tombstone filter silently has nothing to consult/union into,
    # collapsing straight back to the stale-device resurrection bug this fix
    # closes. See app/models/profile.py / app/routers/profile.py.
    ("profiles", "deleted_profile_ids"),
    ("chat_context_chunks", "book_id"), ("chat_context_chunks", "chunk_index"),
    # Personalization questions (Aug 2026). EVERY column the runtime paths
    # actually read or write is listed — audit fix: registering only three
    # of them meant a partially-restored table missing e.g. `options` still
    # reported /ready healthy, then 500'd on the first answer submission
    # (false-green readiness, exactly what this registry exists to prevent).
    ("personalization_questions", "id"),
    ("personalization_questions", "user_id"),
    ("personalization_questions", "daily_bite_id"),
    ("personalization_questions", "library_item_id"),
    ("personalization_questions", "profile_id"),
    ("personalization_questions", "question"),
    ("personalization_questions", "options"),
    ("personalization_questions", "source_chunk_ids"),
    ("personalization_questions", "status"),
    ("personalization_questions", "claimed_until"),
    ("personalization_questions", "claimed_by"),
    ("personalization_questions", "answer_option_id"),
    ("personalization_questions", "answer_free_text"),
    ("personalization_questions", "applied_tags"),
    ("personalization_questions", "interpreted_summary"),
    ("personalization_questions", "answered_at"),
    ("personalization_questions", "created_at"),
]
# Postgres only — a named UNIQUE constraint/index is how each of these
# integrity guarantees is actually enforced by the database, not just
# assumed by application code. Checked via BOTH information_schema.table_
# constraints and pg_indexes because this codebase creates some via `ADD
# CONSTRAINT` and others via `CREATE UNIQUE INDEX` (see the migration
# lists above) — both are valid, and this check doesn't care which style
# a given one used. Each entry is verified as belonging to ITS table, not
# just present somewhere in the database — see verify_required_schema().
REQUIRED_PG_CONSTRAINTS = [
    ("chat_turns", "uq_chat_turn_user_turn"),
    ("daily_bites", "uq_daily_bites_user_item_date"),
    ("saved_bites", "uq_saved_bites_user_bite"),
    ("cleanup_tasks", "uq_cleanup_task_identity_v2"),
    ("delivery_cycles", "uq_delivery_cycle_user_date"),
    ("chat_context_chunks", "uq_chat_context_chunk"),
    ("personalization_questions", "uq_personalization_daily_bite"),
]

# (table, constraint/index name, expected columns) — the subset of the above
# whose COLUMN SHAPE is also verified, not just its existence by name. Each
# of these is a uniqueness guarantee a write path relies on for idempotency
# or dedupe, and a same-named non-unique index (or one on different columns)
# would enforce nothing while passing a name-only check (re-audit #10).
REQUIRED_PG_CONSTRAINT_SHAPES = [
    ("personalization_questions", "uq_personalization_daily_bite", ["daily_bite_id"]),
    ("chat_turns", "uq_chat_turn_user_turn", ["user_id", "turn_id"]),
    ("daily_bites", "uq_daily_bites_user_item_date", ["user_id", "library_item_id", "date"]),
    ("delivery_cycles", "uq_delivery_cycle_user_date", ["user_id", "cycle_date"]),
    # Round-4: the remaining three guarantees now have shape checks too —
    # previously only four of seven did, so the rest were name-only.
    ("saved_bites", "uq_saved_bites_user_bite", ["user_id", "bite_id"]),
    ("cleanup_tasks", "uq_cleanup_task_identity_v2",
     ["item_id", "attempt_token", "artifact_kind", "artifact_key"]),
    ("chat_context_chunks", "uq_chat_context_chunk", ["user_id", "book_id", "chunk_index"]),
]

# Indexes that are legitimately PARTIAL by design. Round-5: a NAME-based
# exemption is not enough — it accepts ANY predicate, including a decoy like
# `WHERE false`, which indexes no rows and therefore enforces nothing while
# looking present, unique, valid and ready. The EXPECTED PREDICATE is
# recorded here and compared against pg_get_expr(indpred, indrelid).
#
# Postgres normalises what it stores, so the comparison is normalised too
# (whitespace collapsed, outer parens and explicit casts stripped) rather
# than demanding a byte-exact string.
_PARTIAL_INDEX_PREDICATES = {
    # legacy pre-session bites carry no item and must not collide
    "uq_daily_bites_user_item_date": "library_item_id IS NOT NULL",
}


def _normalize_predicate(expr: str) -> str:
    """Normalise a pg_get_expr predicate for comparison."""
    if not expr:
        return ""
    s = " ".join(str(expr).split()).lower()
    s = s.replace("::text", "").replace("::character varying", "")
    while s.startswith("(") and s.endswith(")"):
        inner = s[1:-1]
        # only strip when the parens are actually a matching outer pair
        depth = 0
        balanced = True
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        s = inner.strip()
    return s


def verify_required_schema() -> "tuple[bool, list[str]]":
    """Re-derive the EFFECTIVE schema from the database itself and compare
    against REQUIRED_TABLES/REQUIRED_COLUMNS/REQUIRED_PG_CONSTRAINTS, rather
    than trusting that `_run_migrations()`/`_run_task2_required_migrations_and_backfill()`
    not raising is proof the schema is correct — the two are usually the
    same thing, but "the ALTER statement didn't throw" and "the column is
    actually there with the right name" only coincide because no one has
    yet hand-run a migration against the wrong database, restored a partial
    backup mid-migration, or hit a permissions error that a broader except
    clause upstream swallowed. Returns (ok, sorted list of missing items,
    each prefixed "table:"/"column:"/"constraint:" for a readable /ready
    body and log line).
    """
    from sqlalchemy import text

    missing = []
    dialect = engine.dialect.name
    with engine.connect() as conn:
        if dialect == "postgresql":
            existing_tables = {
                row[0] for row in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                ))
            }
            for t in REQUIRED_TABLES:
                if t not in existing_tables:
                    missing.append(f"table:{t}")

            existing_cols = {
                (row[0], row[1]) for row in conn.execute(text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                ))
            }
            for t, c in REQUIRED_COLUMNS:
                if (t, c) not in existing_cols:
                    missing.append(f"column:{t}.{c}")

            # (table, constraint/index name) pairs — NOT just names. An
            # earlier version of this query unioned `pg_constraint.conname`
            # (with no schema filter — pg_constraint spans every schema in
            # the database) against `pg_indexes.indexname`, and only
            # compared names, never confirming the matched object actually
            # belongs to the expected table. `information_schema.table_constraints`
            # carries `table_schema`/`table_name` directly (unlike raw
            # pg_constraint, which needs a manual join through pg_class/
            # pg_namespace to get there), so it replaces the pg_constraint
            # half here — scoped to `public` and paired with its table, the
            # same way the tables/columns checks above already are.
            existing_named = {
                (row[0], row[1]) for row in conn.execute(text(
                    "SELECT table_name, constraint_name FROM information_schema.table_constraints "
                    "WHERE table_schema = 'public' "
                    "UNION "
                    "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'"
                ))
            }
            for t, name in REQUIRED_PG_CONSTRAINTS:
                if (t, name) not in existing_named:
                    missing.append(f"constraint:{t}.{name}")

            # Re-audit finding #10: existence-by-name is not the guarantee
            # these entries stand for. Every one of them is a UNIQUENESS
            # guarantee some write path depends on (idempotency keys, dedupe
            # constraints) — and a plain non-unique index, or a unique
            # constraint on the WRONG columns, satisfies the name check while
            # enforcing nothing. Verify the actual shape where the expected
            # columns are known.
            for t, name, cols in REQUIRED_PG_CONSTRAINT_SHAPES:
                if (t, name) not in existing_named:
                    continue    # already reported missing above
                actual = [
                    row[0] for row in conn.execute(
                        text(
                            "SELECT a.attname "
                            "FROM pg_constraint c "
                            "JOIN pg_class rel ON rel.oid = c.conrelid "
                            "JOIN pg_namespace n ON n.oid = rel.relnamespace "
                            "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
                            "JOIN pg_attribute a ON a.attrelid = rel.oid AND a.attnum = k.attnum "
                            "WHERE n.nspname = 'public' AND rel.relname = :t "
                            "AND c.conname = :name AND c.contype IN ('u', 'p') "
                            "ORDER BY k.ord"
                        ),
                        {"t": t, "name": name},
                    )
                ]
                if not actual:
                    # Not a unique/primary constraint at all — check whether a
                    # UNIQUE INDEX of that name covers the same columns, since
                    # this codebase creates some guarantees that way.
                    #
                    # Round-4: an index only ENFORCES anything when it is
                    # valid, ready and total. A CREATE INDEX CONCURRENTLY that
                    # failed leaves an INVALID index that still appears in the
                    # catalog; a not-ready index isn't enforcing yet; and a
                    # PARTIAL index (indpred) only constrains the rows its
                    # WHERE clause matches, which is not the guarantee the
                    # write paths assume. All three are rejected.
                    #
                    # NOTE: daily_bites' uq_daily_bites_user_item_date IS
                    # deliberately partial (WHERE library_item_id IS NOT NULL)
                    # — see this repo's own prod-DB notes — so it is checked
                    # for validity/readiness but exempted from the
                    # totality requirement.
                    expected_pred = _PARTIAL_INDEX_PREDICATES.get(name)
                    rows = list(conn.execute(
                        text(
                            "SELECT a.attname, i.indisvalid, i.indisready, "
                            "       pg_get_expr(i.indpred, i.indrelid) AS pred "
                            "FROM pg_index i "
                            "JOIN pg_class idx ON idx.oid = i.indexrelid "
                            "JOIN pg_class rel ON rel.oid = i.indrelid "
                            "JOIN pg_namespace n ON n.oid = rel.relnamespace "
                            "JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
                            "JOIN pg_attribute a ON a.attrelid = rel.oid AND a.attnum = k.attnum "
                            "WHERE n.nspname = 'public' AND rel.relname = :t "
                            "AND idx.relname = :name AND i.indisunique "
                            "ORDER BY k.ord"
                        ),
                        {"t": t, "name": name},
                    ))
                    if rows:
                        valid, ready, pred = rows[0][1], rows[0][2], rows[0][3]
                        if not valid:
                            missing.append(f"constraint-invalid:{t}.{name} (index is INVALID)")
                            continue
                        if not ready:
                            missing.append(f"constraint-not-ready:{t}.{name}")
                            continue
                        got_pred = _normalize_predicate(pred)
                        want_pred = _normalize_predicate(expected_pred)
                        # Round-5: the PREDICATE itself is verified, not just
                        # "is it partial". `WHERE false` indexes zero rows and
                        # enforces nothing, while passing every other check.
                        if got_pred != want_pred:
                            missing.append(
                                f"constraint-predicate:{t}.{name} has predicate "
                                f"{got_pred or '(none)'!r}, expected {want_pred or '(none)'!r}"
                            )
                            continue
                    actual = [r[0] for r in rows]
                if sorted(actual) != sorted(cols):
                    missing.append(
                        f"constraint-shape:{t}.{name} covers {sorted(actual) or 'nothing unique'}, "
                        f"expected {sorted(cols)}"
                    )
        else:
            # SQLite (local/test): tables + columns come straight from the
            # ORM via Base.metadata.create_all() — checked the same way, for
            # real coverage, not skipped. Named-constraint introspection is
            # Postgres-only (REQUIRED_PG_CONSTRAINTS): SQLAlchemy renders a
            # SQLite UniqueConstraint as inline CREATE TABLE DDL rather than
            # a separately-named catalog object, so there is nothing to
            # query it back out of the way pg_constraint allows — the model
            # definition + this repo's own persistence tests are what prove
            # that one, not this runtime check.
            existing_tables = {
                row[0] for row in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ))
            }
            for t in REQUIRED_TABLES:
                if t not in existing_tables:
                    missing.append(f"table:{t}")
            for t, c in REQUIRED_COLUMNS:
                if t not in existing_tables:
                    missing.append(f"column:{t}.{c}")
                    continue
                # Table names above are all fixed constants from this
                # module's own registry, never request/user input — safe to
                # interpolate into PRAGMA, which doesn't accept bind params.
                cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({t})"))}
                if c not in cols:
                    missing.append(f"column:{t}.{c}")

    return (len(missing) == 0, sorted(missing))


def get_readiness_status() -> "tuple[bool, dict]":
    """Backing call for `GET /ready` (main.py) — a distinct contract from
    `/health`, which only ever proves the process is alive. Readiness here
    is THREE things, all required: (1) the database is reachable RIGHT NOW
    (a live `SELECT 1` — connectivity can be lost after a clean boot, so
    this is checked per-call, not cached), (2) the schema was verified at
    boot (`_boot_schema_verified` — NOT re-run per call: the schema can't
    change at runtime, and information_schema/pg_catalog round-trips on
    every health-check poll would be wasted cost for zero new signal), and
    (3) the notification scheduler actually started (Task 20 requirement
    #11 — a backend whose scheduler silently failed to initialize would
    otherwise report "ready" while never generating or delivering a single
    nibble). Never includes the DB URL, credentials, or query text — only a
    truncated exception message with no bind parameters, since `SELECT 1`
    carries none to begin with.
    """
    from sqlalchemy import text

    db_reachable = False
    db_error = None
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_reachable = True
    except Exception as e:
        db_error = str(e)[:200]

    try:
        from app.services.notification_service import scheduler_initialized
        scheduler_ready = scheduler_initialized()
    except Exception:
        # A broken import/attribute here is itself a "not ready" signal,
        # not a reason to crash the /ready handler — safe default is False.
        scheduler_ready = False

    ok = db_reachable and _boot_schema_verified and scheduler_ready
    detail = {
        "status": "ready" if ok else "not_ready",
        "db_reachable": db_reachable,
        "schema_verified": _boot_schema_verified,
        "scheduler_ready": scheduler_ready,
    }
    if db_error:
        detail["db_error"] = db_error
    return ok, detail
