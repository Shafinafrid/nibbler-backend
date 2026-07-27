"""
Server-side home for everything that used to live ONLY in the app's
AsyncStorage: notes, highlights, book chats, completions, settings and
transient UI state.

Why this exists (2026-07-25): a reinstall or a new phone silently lost all of
it. The founder's requirement is absolute — "even if the user changes the
phone, or has 100 different iPhones, when they sign in EVERYTHING is still
there." Local storage is now a cache; these tables are the truth.

Sync shape, and why it differs per entity:

  · notes / highlights / chats / completions are **row-per-item, upserted by
    a client-generated id**. Deletions are explicit DELETEs. This matters:
    a blob-replace ("here is my whole list") lets a fresh install with an
    empty cache wipe the server the moment it syncs — exactly the class of
    bug that destroyed a growth profile earlier today. Row upserts can only
    ever ADD; nothing is destroyed without an explicit intent to delete.
  · settings / state are genuinely single-valued per user, so they are blobs —
    but the write path guards them (see routers/sync.py).
"""

import uuid
from sqlalchemy import (
    Column, String, Integer, Boolean, Text, Date, DateTime, JSON, ForeignKey,
    UniqueConstraint, func,
)
from sqlalchemy.orm import relationship
from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Note/highlight identity — read this before changing the keys ────────────
#
# Originally keyed (user_id, book_id, card_index). `card_index` is a card's
# ZERO-BASED POSITION WITHIN ONE DECK (LessonPlayer's `useState(0)`), and every
# daily session for a book reuses 0, 1, 2… So a note on today's card 0 of a
# book OVERWROTE the note on yesterday's card 0 — and the unique constraint
# ENFORCED that collision rather than catching it. Found in the 2026-07-25
# external audit; it pre-dated the sync work (the server faithfully reproduced
# the key the app already used locally, so notes had been quietly eating each
# other on-device long before any of this).
#
# Fixed by adding `daily_bite_id`, which is what actually pins a card to one
# session. Rows written from 2026-07-26 carry it; rows written before cannot
# be attributed to a session after the fact, so they keep the old key and are
# left completely untouched. That means TWO partial unique indexes per table,
# declared in database.py:_run_migrations() rather than here — SQLAlchemy's
# UniqueConstraint can't express a WHERE clause, and declaring an
# unconditional one here would recreate the very bug this fixes.
#
#   ... WHERE daily_bite_id IS NOT NULL  → (user_id, daily_bite_id, card_index)
#   ... WHERE daily_bite_id IS NULL      → (user_id, book_id, card_index)


class Note(Base):
    """A note the user typed against one card of one session."""
    __tablename__ = "notes"

    id = Column(String, primary_key=True, default=_uuid)   # client-generated id
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    book_id = Column(String, nullable=False, index=True)
    # NULL only for rows written before 2026-07-26 — see the note above.
    daily_bite_id = Column(String, nullable=True, index=True)
    book_title = Column(String, nullable=True)
    book_color = Column(String, nullable=True)
    card_index = Column(Integer, nullable=False)
    card_eyebrow = Column(String, nullable=True)
    card_title = Column(String, nullable=True)
    card_body = Column(Text, nullable=True)
    text = Column(Text, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="notes")


class Highlight(Base):
    """A card the user favourited during a session."""
    __tablename__ = "highlights"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    book_id = Column(String, nullable=False, index=True)
    # Same identity story as Note — see the block comment above.
    daily_bite_id = Column(String, nullable=True, index=True)
    book_title = Column(String, nullable=True)
    book_color = Column(String, nullable=True)
    card_index = Column(Integer, nullable=False)
    card_eyebrow = Column(String, nullable=True)
    card_title = Column(String, nullable=True)
    card_body = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="highlights")


class ChatMessage(Base):
    """One message in the Connect-tab conversation with a specific book."""
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    book_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)          # 'user' | 'assistant'
    content = Column(Text, nullable=False)
    # Client millisecond timestamp. Kept alongside created_at because the app
    # orders by it and it survives a replay/re-push unchanged.
    ts = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="chat_messages")


class Completion(Base):
    """One finished reading session. Append-only log; the per-book aggregate
    (how many times, when last) is derived from it."""
    __tablename__ = "completions"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    book_id = Column(String, nullable=False, index=True)
    completed_date = Column(Date, nullable=False)   # the app's local YYYY-MM-DD
    read_length = Column(Integer, nullable=True)    # 5 | 10 | 15
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="completions")


class UserSettings(Base):
    """Everything the user has deliberately configured. One row per user.

    `active_book_ids` / `inactive_order` live here rather than on
    library_items because they are ORDERED LISTS — library_items.is_active
    records *whether* a book feeds nibbles, but not its rank, and rank is
    what the Library screen shows and what the user drags to change.
    """
    __tablename__ = "user_settings"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    read_length = Column(Integer, nullable=True)         # null → onboarding pacing
    delivery_hour = Column(Integer, nullable=True)       # LOCAL hour the user picked
    delivery_minute = Column(Integer, nullable=True)
    daily_quiz = Column(Boolean, default=False)
    streak_alerts = Column(Boolean, default=True)
    dark_mode = Column(Boolean, nullable=True)           # legacy — kept for older clients
    theme_mode = Column(String, nullable=True)            # 'light' | 'dark' | 'black'; null → dark_mode/system
    sound_enabled = Column(Boolean, nullable=True)
    active_book_ids = Column(JSON, nullable=True)        # ordered [bookId] — rank 1 first
    inactive_order = Column(JSON, nullable=True)         # ordered [bookId] — switch-off order
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="settings")


class UserState(Base):
    """Progress/analytics state that isn't a user *setting*: the in-flight
    review run and lifetime quiz counters. One row per user."""
    __tablename__ = "user_state"

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    review_state = Column(JSON, nullable=True)     # deck order, position, per-card results
    quiz_attempts = Column(Integer, default=0)
    quiz_correct = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="state")
