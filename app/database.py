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
        user, profile, library, bite, streak, push_token, user_data, bug_report,
    )
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    _run_task2_required_migrations_and_backfill()


# Arbitrary but stable Postgres advisory-lock key, scoped to this one
# migration+backfill sequence. Session-level (pg_advisory_lock/unlock, not
# the xact variant) and held on ONE connection for the whole block below.
TASK2_ADVISORY_LOCK_KEY = 918_273_645


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
    Safe column-level migrations — adds missing columns without touching
    existing data. Add new ALTER statements here whenever the models gain
    new columns so Railway auto-applies them on next deploy.

    Each statement runs on its OWN autocommit connection so a single failure
    can never poison the rest (a failed statement inside a shared transaction
    aborts every statement after it — which is how production drifted before).
    """
    from sqlalchemy import text

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
        # users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR",
        # profiles — local-first growth state sync (July 2026)
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS growth_state JSON",
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
        # Task 2's own schema (successful_sources_total, free_lock_state_token,
        # is_unlocked_selection, the reservation/provenance columns, and their
        # backfill) moved to TASK2_REQUIRED_MIGRATIONS below — those must
        # succeed or the app refuses to start (fail-closed), unlike every
        # lenient, best-effort statement in this list. The original backfill
        # here (a raw COUNT(*) of every processed row) was ALSO semantically
        # wrong after the entitlement-accounting remediation: it counted
        # Premium-created successes against the Free counter too, which is
        # exactly the bug that remediation fixes — removed rather than left
        # to race the correct Python backfill.
    ]

    applied, failed = 0, 0
    for sql in migrations:
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(sql))
            applied += 1
        except Exception as e:
            failed += 1
            print(f"[migration] FAILED: {sql[:70]}… → {e}")
    print(f"[migration] done: {applied} applied/verified, {failed} failed")
