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
import os
import uuid
import logging
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Optional, List
from app.database import SessionLocal
from pydantic import BaseModel, Field
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite
from app.models.user_data import ChatTurn
from app.rate_limit import limiter
from app.services.llm import LLMService
from app.services.embedding_service import EmbeddingService, EmbeddingError
from app.services import mixpanel_service
from app.services.entitlement_service import is_source_unlocked

router = APIRouter(prefix="/connect", tags=["connect"])
logger = logging.getLogger(__name__)

# ── Chat-turn idempotency (Task 16, Aug 2026) ───────────────────────────────
# 3 minutes is a deliberately generous upper bound on a single chat
# generation (LLMService's own fallback chain can try up to 3 providers plus
# one same-provider retry each — see this backend's own CLAUDE.md), so a
# lease this long is very unlikely to expire out from under a request that
# is genuinely still working, while still self-healing a dead worker inside
# one user-visible retry's timeframe.
CHAT_TURN_LEASE_MINUTES = 3

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
    # Task 16 remediation: a client-generated, per-question id, reused
    # UNCHANGED on Retry (never re-minted). This is what lets this endpoint
    # recognise "I already answered this" or "I'm already working on this"
    # instead of paying for a second generation every time a slow request
    # gets retried. Optional so an old, not-yet-updated client (mid-OTA-
    # rollout) keeps working exactly as before — it just doesn't get the
    # new protection until it updates.
    turn_id: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str


class _ChatTurnClaim:
    """Result of `_claim_chat_turn` — exactly one of these is meaningful:
    `worker_id` set → caller is cleared to generate (fresh turn, a re-
    claimed dead-worker's stale lease, or an explicit retry of a 'failed'
    turn). `replay_reply` set → this exact question was already answered;
    return it verbatim, no LLM call. `in_progress` → a still-live worker
    (this same request racing itself, another device, or a client bug)
    already owns this turn; the caller must not start a second generation.
    `conflict` → this turn id is already bound to a DIFFERENT book OR a
    DIFFERENT question text (Task 16 audit fix) — a client bug or turn-id
    reuse, refused outright rather than guessed at. `conflict_reason`
    distinguishes the two ('book' | 'question') so the client's error
    message doesn't claim the wrong one."""
    def __init__(self, worker_id=None, replay_reply=None, in_progress=False, conflict=False, conflict_reason=None):
        self.worker_id = worker_id
        self.replay_reply = replay_reply
        self.in_progress = in_progress
        self.conflict = conflict
        self.conflict_reason = conflict_reason


def _claim_chat_turn(db: Session, turn_id: str, user_id: str, book_id: str, question: str) -> _ChatTurnClaim:
    """Idempotency/turn-claim primitive for POST /connect/chat (Task 16).

    One row per (user, client-generated turn id) — see ChatTurn's own
    docstring in app/models/user_data.py for the full design rationale.
    ALWAYS commits (releasing the row lock) before returning — the caller
    does the slow, uncancellable LLM call OUTSIDE any lock, exactly like
    every other claim-lease primitive in this codebase (Free-entitlement
    reservations, account erasure, item-deletion cleanup)."""
    now = datetime.utcnow()
    worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    lease_until = now + timedelta(minutes=CHAT_TURN_LEASE_MINUTES)

    turn = (
        db.query(ChatTurn)
        .filter(ChatTurn.turn_id == turn_id, ChatTurn.user_id == user_id)
        .with_for_update()
        .first()
    )

    if turn is not None and turn.book_id != book_id:
        return _ChatTurnClaim(conflict=True, conflict_reason="book")

    # Task 16 audit fix: turn_id alone is not a safe idempotency key — it is
    # a client-generated value, and this table never verified the SAME id
    # was actually still attached to the SAME question. Reusing an id with
    # DIFFERENT text previously either silently replayed an unrelated
    # completed answer (below) or overwrote the original question on a
    # failed/stale-lease claim (further below), corrupting what a still-
    # running worker would eventually attribute to that turn. Bind the id to
    # its original question text, exactly like the pre-existing book_id
    # check above — a text mismatch is refused as a conflict, never guessed at.
    if turn is not None and turn.question is not None and turn.question != question:
        return _ChatTurnClaim(conflict=True, conflict_reason="question")

    if turn is not None and turn.status == "completed":
        return _ChatTurnClaim(replay_reply=turn.reply)

    if turn is not None and turn.status == "pending":
        if turn.claimed_until and turn.claimed_until > now:
            return _ChatTurnClaim(in_progress=True)
        # Lease expired — the worker that claimed it died before finishing.
        # Re-claim for THIS attempt (crash recovery). question is already
        # verified identical above — nothing to overwrite there.
        turn.claimed_by = worker_id
        turn.claimed_until = lease_until
        db.commit()
        return _ChatTurnClaim(worker_id=worker_id)

    if turn is not None and turn.status == "failed":
        # Explicit retry of a definitively-failed turn — re-open it for
        # exactly one more attempt, same claim as a fresh turn. question is
        # already verified identical above.
        turn.status = "pending"
        turn.claimed_by = worker_id
        turn.claimed_until = lease_until
        turn.error_code = None
        turn.error_message = None
        db.commit()
        return _ChatTurnClaim(worker_id=worker_id)

    # No existing row at all — a brand-new turn. `id` is left to its
    # server-minted default; `turn_id` (not `id`) is the client's own value
    # — see ChatTurn's docstring for why the two must never be the same
    # column (a client-generated value cannot safely be a global PK).
    turn = ChatTurn(
        turn_id=turn_id, user_id=user_id, book_id=book_id, status="pending",
        question=question, claimed_by=worker_id, claimed_until=lease_until,
    )
    db.add(turn)
    try:
        db.commit()
    except IntegrityError:
        # Two near-simultaneous requests for a turn id that didn't exist
        # yet when both SELECTs ran (a genuine duplicate tap racing the
        # client's own in-flight guard). Whoever loses the insert re-reads
        # under lock and defers to whoever won, rather than erroring out.
        db.rollback()
        turn = (
            db.query(ChatTurn)
            .filter(ChatTurn.turn_id == turn_id, ChatTurn.user_id == user_id)
            .with_for_update()
            .first()
        )
        if turn is None:
            raise
        if turn.status == "completed":
            return _ChatTurnClaim(replay_reply=turn.reply)
        return _ChatTurnClaim(in_progress=True)
    return _ChatTurnClaim(worker_id=worker_id)


# ── Lease heartbeat around the LLM call (Task 16 audit fix) ────────────────
# CHAT_TURN_LEASE_MINUTES was previously a FIXED lease with nothing renewing
# it while the worker was blocked inside the single opaque llm.chat_with_
# book() call. If generation legitimately ran longer than the lease (a slow
# provider, or LLMService's own multi-provider fallback chain genuinely
# working through 2-3 attempts), the lease could expire WHILE the first
# worker was still alive and mid-call — a user retry then re-claimed the
# turn and started a SECOND concurrent paid generation, with only the
# ownership check in _complete_chat_turn/_fail_chat_turn (ChatTurn.
# claimed_by != worker_id) stopping the loser from overwriting the winner's
# result. That check prevents a corrupted final write but does nothing to
# stop the wasted paid call itself. This mirrors _AttemptGuard's background-
# renewal half (see routers/library.py) but is deliberately much smaller:
# chat has no natural per-chunk checkpoint to call renew_now() from inside
# a single blocking LLM call, so only the background-thread renewal side
# applies here.
_CHAT_HEARTBEAT_INTERVAL_SECONDS = 30.0


class _ChatTurnHeartbeat:
    """Background thread that extends a ChatTurn's claimed_until on a fixed
    interval for as long as the worker is blocked inside the LLM call.
    Renews only while THIS worker still owns the lease (authoritative
    per-tick DB check, not a cached flag) — if ownership was already lost
    (a bug elsewhere, or manual intervention), the thread stops renewing
    and records the loss rather than fighting whoever now owns the row.
    Runs through its own independent SessionLocal(), never the request's
    own `db` (that connection is idle on this thread's stack, not safe to
    share across threads)."""

    def __init__(self, turn_id: str, user_id: str, worker_id: str,
                 interval_seconds: float = None):
        self.turn_id = turn_id
        self.user_id = user_id
        self.worker_id = worker_id
        # Read the module-level constant at CALL time, not as a Python
        # default-parameter value bound once at function-definition time —
        # the latter can't be overridden by a test's mock.patch on the
        # module attribute after this class is already defined.
        self.interval_seconds = interval_seconds if interval_seconds is not None else _CHAT_HEARTBEAT_INTERVAL_SECONDS
        self._stop = threading.Event()
        self._thread = None
        self.lost = False

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            db = SessionLocal()
            try:
                turn = (
                    db.query(ChatTurn)
                    .filter(ChatTurn.turn_id == self.turn_id, ChatTurn.user_id == self.user_id)
                    .with_for_update()
                    .first()
                )
                if not turn or turn.claimed_by != self.worker_id:
                    self.lost = True
                    return
                turn.claimed_until = datetime.utcnow() + timedelta(minutes=CHAT_TURN_LEASE_MINUTES)
                db.commit()
            except Exception:
                logger.exception("Chat-turn heartbeat renewal failed for turn %s", self.turn_id)
            finally:
                db.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.critical("CHAT_HEARTBEAT_JOIN_FAILED turn=%s worker=%s — thread did not "
                                 "stop within timeout", self.turn_id, self.worker_id)


def _complete_chat_turn(db: Session, turn_id: str, user_id: str, worker_id: str, reply: str) -> None:
    """Write the canonical result — only if THIS worker still owns the
    lease. A worker whose generation ran long enough for its lease to
    expire and be reclaimed by a newer attempt must not overwrite that
    newer attempt's in-progress or already-completed state with its own
    late, stale result (the same TOCTOU-safety shape as every other
    claim-lease writer in this codebase)."""
    turn = (
        db.query(ChatTurn)
        .filter(ChatTurn.turn_id == turn_id, ChatTurn.user_id == user_id)
        .with_for_update()
        .first()
    )
    if not turn or turn.claimed_by != worker_id:
        return
    turn.status = "completed"
    turn.reply = reply
    turn.error_code = None
    turn.error_message = None
    db.commit()


def _fail_chat_turn(db: Session, turn_id: str, user_id: str, worker_id: str, error_code: str, error_message: str) -> None:
    """Same lease-ownership guard as `_complete_chat_turn` — see its
    docstring. `error_message` must already be the exact, user-safe text
    the client will display; never provider internals or a raw exception."""
    turn = (
        db.query(ChatTurn)
        .filter(ChatTurn.turn_id == turn_id, ChatTurn.user_id == user_id)
        .with_for_update()
        .first()
    )
    if not turn or turn.claimed_by != worker_id:
        return
    turn.status = "failed"
    turn.error_code = error_code
    turn.error_message = error_message
    db.commit()


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
        LibraryItem.deletion_state.is_(None),  # a tombstoned item is gone
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if not item.processed:
        raise HTTPException(status_code=409, detail="Nibbler is still reading this one.")
    if not is_source_unlocked(user, item):
        # Covers /connect/insights, /connect/stats/{id} and /connect/chat in
        # one place — every caller routes through this helper (Task 2).
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now. Upgrade to chat with it again.",
            },
        )
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

    # Task 16 remediation: claim/replay/reject BEFORE any paid work begins —
    # no ChatTurn row is ever created for a request that was always going to
    # be refused on entitlement/validation grounds (mirrors this backend's
    # own Free-entitlement convention of reserving capacity only after
    # every non-cost check has already passed).
    turn_id = data.turn_id or str(uuid.uuid4())
    claim = _claim_chat_turn(db, turn_id, current_user.id, item.id, message)
    if claim.conflict:
        conflict_message = (
            "This question's id is already associated with a different book."
            if claim.conflict_reason == "book" else
            "This question's id is already associated with different question text — try again."
        )
        raise HTTPException(status_code=409, detail={
            "code": "chat_turn_conflict",
            "message": conflict_message,
        })
    if claim.replay_reply is not None:
        # Idempotent replay: the exact same question, already answered —
        # a retry after the client never heard back, or a duplicate tap.
        # No embeddings call, no LLM call.
        return ChatResponse(reply=claim.replay_reply)
    if claim.in_progress:
        raise HTTPException(status_code=409, detail={
            "code": "chat_turn_processing",
            "message": "Still working on your last question — hang tight.",
        })
    worker_id = claim.worker_id

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
        no_content_msg = "No indexed content found for this book."
        _fail_chat_turn(db, turn_id, current_user.id, worker_id, "no_content", no_content_msg)
        raise HTTPException(status_code=422, detail={"code": "no_content", "message": no_content_msg})

    llm = LLMService()
    # Task 16 audit fix: keep the lease alive for as long as this worker is
    # genuinely still blocked inside the LLM call — see _ChatTurnHeartbeat's
    # own docstring for why a fixed lease alone let a legitimately-slow
    # generation get raced by a user retry.
    heartbeat = _ChatTurnHeartbeat(turn_id, current_user.id, worker_id)
    heartbeat.start()
    try:
        reply = llm.chat_with_book(
            book_title=item.title,
            author=item.author,
            excerpts=excerpts,
            history=data.history,
            message=message,
        )
    except Exception:
        # Never leak the raw exception to the client (provider internals,
        # model names, stack detail) — a fixed, safe message only.
        logger.exception("Connect chat generation failed for turn %s", turn_id)
        gen_failed_msg = "Something went wrong generating a reply — you can try again."
        heartbeat.stop()
        _fail_chat_turn(db, turn_id, current_user.id, worker_id, "generation_failed", gen_failed_msg)
        raise HTTPException(status_code=502, detail={"code": "generation_failed", "message": gen_failed_msg})
    heartbeat.stop()

    _complete_chat_turn(db, turn_id, current_user.id, worker_id, reply)
    background_tasks.add_task(mixpanel_service.track, "book_chat_message", current_user.id, {
        "item_id": item.id, "mode": item.mode or "wisdom",
    })
    return ChatResponse(reply=reply)
