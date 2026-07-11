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
    from app.models import user, profile, library, bite, streak, push_token  # noqa
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
        # users
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR",
        # profiles — local-first growth state sync (July 2026)
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS growth_state JSON",
        # push_tokens — minute-precision delivery (July 2026)
        "ALTER TABLE push_tokens ADD COLUMN IF NOT EXISTS notification_minute INTEGER DEFAULT 0",
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
