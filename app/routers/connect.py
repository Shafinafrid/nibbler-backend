"""
Connect — chat with your own books (premium).

  POST /connect/insights  → per-book goal-match analytics: relevance score
                            (vector similarity between the user's growth
                            profile and the book's chunks) + top passages.
  GET  /connect/stats/{id}→ HONEST per-book reading stats, straight from the
                            server's own read receipts: unique sessions read,
                            total sessions this book can produce, explored %
                            (distinct chunks actually read / all chunks), and
                            the latest READ nibble's goal passage with its
                            real date. The app must never invent these.
  POST /connect/chat      → grounded chat: Claude answers ONLY from excerpts of
                            this book (retrieved per question), and says so when
                            the answer isn't in the book.
"""
import math

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel, Field
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite
from app.rate_limit import limiter
from app.services.claude import ClaudeService
from app.services.embedding_service import EmbeddingService, EmbeddingError
from app.services import mixpanel_service

router = APIRouter(prefix="/connect", tags=["connect"])

# ── Goal-match calibration ────────────────────────────────────────────────────
# Measured July 2026 on real voyage-3-lite query→document cosines against an
# uploaded book (The Intelligent Investor, 717 chunks):
#   on-topic goal ("understand money & investing")  top-5 avg ≈ 0.43–0.52
#   adjacent goal ("build better habits")           top-5 avg ≈ 0.25–0.32
#   unrelated goal ("learn italian cooking")        top-5 avg ≈ 0.16–0.23
# The previous linear map assumed on-topic ≈ 0.55–0.75, which voyage-3-lite
# simply never produces — a perfectly on-topic book displayed ~20% (or the 4%
# floor when the vectors were mock-poisoned). These anchors put an on-topic
# book at ~90–100, adjacent at ~30–50, unrelated under ~20.
_RELEVANCE_ANCHORS = [
    (0.18, 6), (0.25, 25), (0.32, 45), (0.40, 78), (0.47, 95), (0.52, 100),
]


def _relevance_pct(avg_score: float) -> int:
    """Piecewise-linear map from top-5 avg cosine to a user-facing percent."""
    first_x, first_y = _RELEVANCE_ANCHORS[0]
    if avg_score <= first_x:
        return max(2, round(first_y * max(avg_score, 0) / first_x))
    for (x1, y1), (x2, y2) in zip(_RELEVANCE_ANCHORS, _RELEVANCE_ANCHORS[1:]):
        if avg_score <= x2:
            return round(y1 + (y2 - y1) * (avg_score - x1) / (x2 - x1))
    return 100


class ConnectProfile(BaseModel):
    name: Optional[str] = None
    lifeArea: Optional[str] = None
    aspirationLabel: Optional[str] = None
    aspirationUnderstanding: Optional[str] = None
    interests: Optional[List[str]] = None


class InsightsRequest(BaseModel):
    library_item_id: str
    growth_profile: Optional[ConnectProfile] = None


class InsightsResponse(BaseModel):
    relevance_pct: int
    relevance_band: str
    top_passages: List[str]
    chunk_count: int
    mode: str


class GoalPassage(BaseModel):
    text: str
    date: str  # YYYY-MM-DD of the nibble it came from — shown honestly in the app


class BookStatsResponse(BaseModel):
    sessions_read: int      # unique sessions COMPLETED (server read receipts — re-reads can't inflate)
    sessions_total: int     # how many nibbles this book can produce in total
    explored_pct: int       # distinct chunks actually read / all chunks
    chunk_count: int
    goal_passage: Optional[GoalPassage] = None


class ChatRequest(BaseModel):
    library_item_id: str
    # Caps keep a single request's Claude cost bounded; the service only uses
    # the last 8 history turns anyway.
    message: str = Field(..., max_length=2000)
    history: List[dict] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    reply: str


def _require_premium(user: User):
    """Connect is a Premium feature (PRD §5): free users see the paywall.
    Structured detail so the app can route to the paywall by code."""
    if not user.effective_premium:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "premium_required",
                "message": "Chatting with your books is a Premium feature.",
            },
        )


def _get_item(item_id: str, user: User, db: Session) -> LibraryItem:
    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == user.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if not item.processed:
        raise HTTPException(status_code=409, detail="Nibbler is still reading this one.")
    return item


@router.post("/insights", response_model=InsightsResponse)
@limiter.limit("30/hour")
def get_insights(
    request: Request,
    data: InsightsRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_premium(current_user)
    item = _get_item(data.library_item_id, current_user, db)

    def _unknown() -> InsightsResponse:
        # The app shows "analytics will be ready in a moment" for this band and
        # refuses to cache it — a transient failure must never stick, and a
        # made-up number must never render.
        return InsightsResponse(
            relevance_pct=0, relevance_band="Unknown",
            top_passages=[], chunk_count=item.chunk_count or 0,
            mode=item.mode or "wisdom",
        )

    profile = data.growth_profile
    query_bits = []
    if profile:
        query_bits = [
            profile.aspirationUnderstanding or profile.aspirationLabel or "",
            " ".join(profile.interests or []),
            profile.lifeArea or "",
        ]
    query = " ".join(b for b in query_bits if b).strip() or "personal growth and learning"

    embeddings = EmbeddingService()
    try:
        scored = embeddings.search_item_scored(
            query=query, user_id=current_user.id, item_id=item.id, top_k=8,
        )
    except EmbeddingError:
        # Voyage is down / rate-limited right now — a paid analytics card must
        # never show a fabricated number because of an infra hiccup.
        return _unknown()

    if not scored:
        return _unknown()

    if any(s["embedder"] != "voyage" for s in scored):
        # This book's stored vectors are dev-mock garbage (Voyage failed during
        # ingestion and old code silently indexed random vectors — cosine ≈ 0
        # → the absurd "4% match"). Re-embed from the stored text in the
        # background and report "in a moment" instead of a lie.
        if item.content:
            from app.routers.library import process_item_embeddings
            background_tasks.add_task(process_item_embeddings, item.id, current_user.id)
        return _unknown()

    top5 = [s["score"] for s in scored[:5]]
    avg = sum(top5) / len(top5)
    pct = _relevance_pct(avg)
    band = "Strong match" if pct >= 65 else "Good match" if pct >= 40 else "Side quest"

    # The passages that speak most to their goal (trimmed for card display)
    passages = []
    for s in scored[:3]:
        t = " ".join(s["text"].split())
        passages.append(t[:220] + ("…" if len(t) > 220 else ""))

    return InsightsResponse(
        relevance_pct=pct,
        relevance_band=band,
        top_passages=passages,
        chunk_count=item.chunk_count or 0,
        mode=item.mode or "wisdom",
    )


@router.get("/stats/{library_item_id}", response_model=BookStatsResponse)
def get_book_stats(
    library_item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Honest reading stats from the server's own records — no client guesses.

    · sessions_read: bites with read_at set (one row per session → re-reading
      the same session can never bump the count)
    · explored_pct: distinct chunk indexes across READ sessions over the whole
      book — a 5-page article and a 1000-page book fill it at honest speeds
    · sessions_total: derived from chunk_count / avg chunks-per-session, known
      the moment the book finishes processing
    · goal_passage: from the most recent READ nibble, with its real date
    """
    _require_premium(current_user)
    item = _get_item(library_item_id, current_user, db)

    bites = (
        db.query(DailyBite)
        .filter(
            DailyBite.user_id == current_user.id,
            DailyBite.library_item_id == item.id,
        )
        .order_by(DailyBite.date.desc())
        .all()
    )
    read = [b for b in bites if b.read_at is not None]

    chunk_count = item.chunk_count or 0

    read_chunks: set = set()
    for b in read:
        read_chunks.update(i for i in (b.chunk_ids or []) if isinstance(i, int))

    # Chunks-per-session: average of what sessions actually drew, falling back
    # to the 5-minute default (6) before any session exists.
    sized = [len(b.chunk_ids) for b in bites if b.chunk_ids]
    per_session = (sum(sized) / len(sized)) if sized else 6
    sessions_total = max(1, math.ceil(chunk_count / per_session)) if chunk_count else max(1, len(read))

    if chunk_count:
        explored = round(len(read_chunks) / chunk_count * 100)
        # Pre-chunk_ids sessions (legacy rows) still count for a floor estimate
        legacy_read = [b for b in read if not b.chunk_ids]
        if legacy_read and explored < 100:
            explored = min(100, explored + round(len(legacy_read) * per_session / chunk_count * 100))
    else:
        explored = 0

    goal_passage = None
    for b in read:  # newest first
        if b.goal_passage:
            goal_passage = GoalPassage(text=b.goal_passage, date=b.date.isoformat())
            break

    return BookStatsResponse(
        sessions_read=len(read),
        sessions_total=max(sessions_total, len(read)),
        explored_pct=max(0, min(100, explored)),
        chunk_count=chunk_count,
        goal_passage=goal_passage,
    )


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("20/hour")
def chat(
    request: Request,
    data: ChatRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_premium(current_user)
    message = (data.message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message is empty.")

    item = _get_item(data.library_item_id, current_user, db)

    embeddings = EmbeddingService()
    try:
        excerpts = embeddings.search_item(
            query=message, user_id=current_user.id, item_id=item.id, top_k=8,
        )
    except EmbeddingError:
        excerpts = []  # Voyage hiccup — fall through to the raw-text fallback
    if not excerpts and item.content:
        excerpts = [item.content[:8000]]
    if not excerpts:
        raise HTTPException(status_code=422, detail="No indexed content found for this book.")

    claude = ClaudeService(is_premium=current_user.effective_premium)
    try:
        reply = claude.chat_with_book(
            book_title=item.title,
            author=item.author,
            excerpts=excerpts,
            history=data.history,
            message=message,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chat failed: {e}")

    background_tasks.add_task(mixpanel_service.track, "book_chat_message", current_user.id, {
        "item_id": item.id, "mode": item.mode or "wisdom",
    })
    return ChatResponse(reply=reply)
