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
