"""
Shared nibble-session generation.

Used by BOTH the on-demand HTTP handler (`POST /bites/session`) and the
scheduler that pre-generates the daily nibble(s) ~5 minutes before the user's
delivery time (see notification_service). Keeping one code path means the
"tap a book" flow and the "delivered at your time" flow produce identical decks.

This module is HTTP-agnostic: it raises SessionGenerationError (with a
suggested status_code) instead of FastAPI HTTPException, so the scheduler can
use it without a request context.
"""

import uuid
import logging
from datetime import date as date_cls
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.bite import DailyBite
from app.models.library import LibraryItem
from app.models.user import User
from app.services.claude import ClaudeService
from app.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

# read length → total cards in the deck / retrieval breadth / story words
CARD_TARGETS = {5: 5, 10: 8, 15: 12}
WISDOM_TOP_K = {5: 6, 10: 10, 15: 14}
STORY_WORDS = {5: 1100, 10: 2200, 15: 3300}


class SessionGenerationError(Exception):
    """A session couldn't be generated (bad input, retrieval empty, Claude failure)."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _profile_query(profile: dict) -> str:
    query_bits = [
        profile.get("aspirationUnderstanding") or profile.get("aspirationLabel") or "",
        " ".join(profile.get("interests") or []),
        profile.get("lifeArea") or "",
    ]
    return " ".join(b for b in query_bits if b).strip()


def generate_session_for_item(
    db: Session,
    *,
    user: User,
    item: LibraryItem,
    read_length: int,
    profile: dict,
    today: date_cls,
    origin: str = "manual",
) -> DailyBite:
    """
    Generate and persist one nibble session for (user, item, today), returning
    the DailyBite. If a concurrent write already created it (unique index on
    user/item/date), returns the existing winner instead of raising.

    Does NOT enforce daily caps or dedupe an already-generated session — callers
    own those pre-checks (the HTTP handler and the scheduler differ there).
    """
    read_length = read_length if read_length in CARD_TARGETS else 5
    is_premium = user.effective_premium
    claude = ClaudeService(is_premium=is_premium)
    mode = item.mode or "wisdom"
    card_target = CARD_TARGETS[read_length]
    story_finished = False
    goal_passage = None

    if mode == "story":
        words = (item.content or "").split()
        if not words:
            raise SessionGenerationError("No readable text stored for this book.", 422)
        progress = item.story_progress or 0
        if progress >= len(words):
            story_finished = True
            result = {
                "title": "The end — you finished it!",
                "chapter": "THE END",
                "headline": f"You've read all of {item.title}.",
                "preview": "Every last page, one daily portion at a time.",
                "cards": [{
                    "kind": "summary",
                    "eyebrow": "THE END",
                    "title": f"You finished {item.title}.",
                    "body": "That's the whole book — read the way books are meant to be read: steadily, in order, without losing the thread.\n\nAdd another story to your library to start your next journey.",
                }],
                "quiz": None,
            }
        else:
            n = STORY_WORDS[read_length]
            excerpt = " ".join(words[progress:progress + n])
            part_number = progress // n + 1
            try:
                result = claude.generate_story_session(
                    book_title=item.title, author=item.author,
                    excerpt=excerpt, card_target=max(3, card_target - 1),
                    part_number=part_number,
                )
            except Exception as e:
                raise SessionGenerationError(f"Session generation failed: {e}", 502)
            item.story_progress = min(progress + n, len(words))
    else:
        profile = profile or {}
        pq = _profile_query(profile)
        query = pq or item.title
        embeddings = EmbeddingService()
        chunks = embeddings.search_item(
            query=query, user_id=user.id, item_id=item.id,
            top_k=WISDOM_TOP_K[read_length],
        )
        # Retrieval is ranked by similarity to the growth profile, so the top
        # chunk IS today's most goal-relevant passage (Connect tab uses it).
        if chunks and pq:
            goal_passage = " ".join(chunks[0].split())
            goal_passage = goal_passage[:280] + ("…" if len(goal_passage) > 280 else "")
        if not chunks and item.content:
            chunks = [item.content[:8000]]  # Pinecone down — fall back to raw text
        if not chunks:
            raise SessionGenerationError("No indexed content found for this item.", 422)
        try:
            result = claude.generate_wisdom_session(
                book_title=item.title, author=item.author,
                profile=profile, context_chunks=chunks,
                card_target=card_target, read_length=read_length,
            )
        except Exception as e:
            raise SessionGenerationError(f"Session generation failed: {e}", 502)

    bite = DailyBite(
        id=str(uuid.uuid4()),
        user_id=user.id,
        library_item_id=item.id,
        date=today,
        title=(result.get("title") or item.title)[:250],
        insight=result.get("preview") or result.get("headline") or "",
        reflection="",
        action="",
        source=item.title,
        theme="story_finished" if story_finished else mode,
        cards=result.get("cards") or [],
        quiz=result.get("quiz"),
        read_length=read_length,
        mode=mode,
        chapter=(result.get("chapter") or "")[:250],
        headline=(result.get("headline") or "")[:500],
        preview=result.get("preview") or "",
        goal_passage=goal_passage,
        origin=origin,
    )
    db.add(bite)
    try:
        db.commit()
    except IntegrityError:
        # Unique index on (user, item, date): a concurrent request won — return it.
        db.rollback()
        winner = db.query(DailyBite).filter(
            DailyBite.user_id == user.id,
            DailyBite.library_item_id == item.id,
            DailyBite.date == today,
        ).first()
        if winner:
            return winner
        raise
    db.refresh(bite)
    return bite
