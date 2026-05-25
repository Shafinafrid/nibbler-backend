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
    """
    migrations = [
        # library_items — columns added with embedding pipeline (May 2026)
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS chunk_count INTEGER DEFAULT 0",
        "ALTER TABLE library_items ADD COLUMN IF NOT EXISTS processing_error VARCHAR",
    ]

    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(__import__('sqlalchemy').text(sql))
                conn.commit()
            except Exception as e:
                # Table might not exist yet on a brand-new deploy — that's fine,
                # create_all() above will create it with the right schema.
                print(f"[migration] skipped ({e})")
