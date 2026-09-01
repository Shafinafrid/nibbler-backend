"""
Dynamic growth-profile personalization questions (Aug 2026).

Occasionally, a wisdom-mode nibble session ends with one extra card that
asks the user a grounded, book-specific preference question — e.g. for a
"Financial enrichment" profile fed by The Intelligent Investor: "Do you
enjoy digging through spreadsheets for stock picks, or would you rather
automate it?" The answer feeds the SAME growth profile the book is active
on, turning onboarding's one-time self-report into something that keeps
sharpening from what the user actually reads and chooses.

One row per (user, DailyBite) — created at GENERATION time (status
'pending'), not at answer time — because that's what makes the answer
endpoint idempotent under replay: `daily_bite_id` is unique, so a second
device (or a retried request) submitting an answer against an already-
'answered' row can be told "already recorded" instead of double-applying
profile deltas. See app/routers/bites.py's personalize-answer endpoint and
app/services/session_service.py's `_roll_personalization`.

This table is the SOURCE OF TRUTH for whether a specific question has been
answered. The app also mirrors a lightweight history into its own local
growth_state blob (ProfileRepository.js's `personalizationHistory`) purely
as a fast read cache for the timeline UI — that local copy is never
authoritative, since a last-writer-wins JSON blob push can't express
"already answered, reject a second submission" the way this table's
`status` + unique constraint can.
"""
from sqlalchemy import (
    Column, String, Text, DateTime, JSON, ForeignKey, UniqueConstraint, Index, func,
)
from app.database import Base
from app.models.user_data import _uuid


class PersonalizationQuestion(Base):
    __tablename__ = "personalization_questions"
    __table_args__ = (
        # The whole idempotency anchor (see module docstring): one
        # personalization card per DailyBite, so the answer endpoint can
        # look this row up unambiguously and a replayed/duplicate submit is
        # detectable as "already answered" rather than silently creating a
        # second record.
        UniqueConstraint("daily_bite_id", name="uq_personalization_daily_bite"),
        Index("ix_personalization_user_created", "user_id", "created_at"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    daily_bite_id = Column(String, ForeignKey("daily_bites.id", ondelete="CASCADE"), nullable=False)
    # No FK on these two — matches daily_bites.library_item_id's own
    # convention: a deleted book or a profile the user later renames/deletes
    # must not block or crash a read of this historical row.
    library_item_id = Column(String, nullable=True)
    profile_id = Column(String, nullable=True)  # the local growth-profile id this targeted, if the app sent one
    question = Column(Text, nullable=False)
    # [{id, text, tag}, ...] — `tag` is one of app/services/llm/schemas.py's
    # PERSONALIZATION_TAGS, the fixed vocabulary the deterministic
    # tag→delta mapping (PERSONALIZATION_TAG_DELTAS) understands. Never a
    # raw numeric delta from the model itself — see that module's docstring
    # for why.
    options = Column(JSON, nullable=False)
    # Chunk indexes the question was grounded in — provenance only, mirrors
    # DailyBite.chunk_ids; never a query-time filter.
    source_chunk_ids = Column(JSON, nullable=True)
    # pending | processing | answered. 'processing' means a request has
    # CLAIMED this row and is resolving the answer (possibly inside a slow
    # LLM call) — see the personalize-answer endpoint. A claim that outlives
    # `claimed_until` is treated as a dead worker's and may be taken over,
    # matching ChatTurn's lease idiom rather than inventing a new one.
    status = Column(String, nullable=False, default="pending", index=True)
    # WHO holds the claim, not just until when. Without this pair being
    # checked together, finalize can only ask "has anyone answered yet",
    # which cannot tell a fresh row apart from one a NEWER worker is
    # currently mid-flight on — so a superseded slow worker would overwrite
    # the newer worker's answer and clear its lease. Same reason ChatTurn
    # carries claimed_by (app/models/user_data.py).
    claimed_by = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)
    answer_option_id = Column(String, nullable=True)
    answer_free_text = Column(Text, nullable=True)
    # The resolved tag(s) actually applied — from the fixed option, or from
    # interpret_personalization_answer() for the free-text path.
    applied_tags = Column(JSON, nullable=True)
    interpreted_summary = Column(Text, nullable=True)  # free-text path only
    answered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
