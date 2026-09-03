from sqlalchemy import Column, String, ForeignKey, DateTime, Text, Date, Integer, JSON, func
from sqlalchemy.orm import relationship
from app.database import Base


class DailyBite(Base):
    __tablename__ = "daily_bites"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    insight = Column(Text, nullable=False)
    reflection = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    source = Column(String, nullable=True)
    theme = Column(String, nullable=True)
    date = Column(Date, nullable=False)          # The date this bite is for
    # ── Session fields (July 2026): per-book card-deck sessions ──
    library_item_id = Column(String, nullable=True, index=True)
    cards = Column(JSON, nullable=True)          # full card deck for the session
    quiz = Column(JSON, nullable=True)           # book-specific quiz (next-day daily quiz + review)
    read_length = Column(Integer, nullable=True) # 5 | 10 | 15 minutes
    mode = Column(String, nullable=True)         # wisdom | story
    chapter = Column(String, nullable=True)      # display line for the home card
    headline = Column(String, nullable=True)
    preview = Column(Text, nullable=True)
    goal_passage = Column(Text, nullable=True)   # this nibble's most goal-relevant excerpt (Connect tab)
    # WHICH growth profile that goal passage was written for (Sep 2026).
    # Without it, Connect showed a passage chosen for a previous assignment
    # as though it spoke to the book's current goal. Stamped by both the
    # on-demand and scheduler generation paths; NULL on legacy rows, which
    # /connect/stats hides rather than guessing at.
    growth_profile_id = Column(String, nullable=True)
    chunk_ids = Column(JSON, nullable=True)      # chunk indexes this session drew from — drives honest Explored % + no-repeat retrieval
    generated_at = Column(DateTime, server_default=func.now())
    # ── Session lifecycle (July 2026): scheduled generation + hold-until-read ──
    origin = Column(String, nullable=True, default="manual")  # 'scheduled' (pre-generated at delivery time) | 'manual' (user tapped a book)
    read_at = Column(DateTime, nullable=True)    # when the user finished reading; NULL = unread/held
    # ── Generation claim/lease (finding #5, Aug 2026) ──────────────────────
    # Closes the gap where two concurrent POST /bites/session requests (a
    # double-tap, or a client retrying a slow/timed-out call) for the SAME
    # (user_id, library_item_id, date) both ran generate_session_for_item all
    # the way through and both paid for the LLM call — only deduping the
    # STORED ROW afterwards via uq_daily_bites_user_item_date's IntegrityError,
    # by which point the expensive generation had already happened twice.
    #
    # A placeholder row is now inserted (this same unique index) BEFORE any
    # LLM call, with cards left NULL — reusing the sentinel every read path
    # already treats as "not a real generated session" (see `existing.cards`
    # in bites.py's get_or_create_session, the `.isnot(None)` cap-counting
    # filter, and `if r.cards` in get_session_history — none of them needed
    # to change). claimed_by/claimed_until are the SAME lease idiom as
    # PersonalizationQuestion's answer-claim and DeliveryCycle's generation/
    # push claim (see those models' docstrings) — an atomic conditional
    # UPDATE checked by rowcount, never an inspect-then-write on a loaded ORM
    # object (round-4's personalization fix found the latter silently
    # doesn't work under SQLAlchemy's identity map). A lease that outlives
    # claimed_until (crashed/hung worker) is reclaimable by a later request;
    # finalize/release only ever succeed while `claimed_by` still matches the
    # calling worker.
    #
    # This ONE primitive protects BOTH callers of generate_session_for_item —
    # the on-demand HTTP path (bites.py's get_or_create_session) AND the
    # scheduler's DeliveryCycle-claimed generation phase
    # (delivery_lifecycle.py's process_generation_phase) — because the claim
    # lives at the top of generate_session_for_item itself, which both
    # eventually call into, rather than being reimplemented per caller.
    claimed_by = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="daily_bites")


class SavedBite(Base):
    __tablename__ = "saved_bites"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bite_id = Column(String, ForeignKey("daily_bites.id", ondelete="CASCADE"), nullable=False)
    saved_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="saved_bites")
    bite = relationship("DailyBite")
