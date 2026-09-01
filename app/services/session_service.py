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

import os
import random
import re
import threading
import time
import uuid
import logging
from datetime import date as date_cls, datetime, timedelta
from typing import List, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models.bite import DailyBite
from app.models.library import LibraryItem
from app.models.personalization import PersonalizationQuestion
from app.models.user import User
from app.services.llm import LLMService
from app.services.embedding_service import EmbeddingService
from app.services import image_select
from app.services.entitlement_service import is_source_unlocked, touch_last_active

logger = logging.getLogger(__name__)

# ── Generation claim/lease (finding #5, Aug 2026) ───────────────────────────
# Protects the expensive LLM call(s) inside generate_session_for_item, not
# just the final row — see DailyBite.claimed_by's docstring in
# app/models/bite.py for the full rationale. Idiom matches
# bites.py's PersonalizationQuestion lease and delivery_lifecycle.py's
# DeliveryCycle lease exactly: atomic conditional UPDATEs checked by
# rowcount, never inspect-then-write on a loaded ORM object.

# Generous enough that a genuinely slow generation (LLM call + retries,
# possibly a second personalization-question call first) is never raced by
# a legitimate retry; short enough that a crashed/hung worker self-heals
# inside one user retry. Matches PERSONALIZE_CLAIM_MINUTES' reasoning in
# bites.py, widened because this claim can cover TWO LLM calls, not one.
GENERATION_CLAIM_MINUTES = 4
# How long the LOSING request (the one that hit IntegrityError on the claim
# insert) polls for the winner to finish, before giving up and returning a
# retryable error. Bounded because this is a real HTTP request — a client
# that gives up waiting can always tap again. Well under the claim lease
# itself, so a poll can only ever observe "still generating" or "done", not
# outlive a legitimately-still-working winner and falsely time out.
GENERATION_POLL_TIMEOUT_SECONDS = 20.0
GENERATION_POLL_INTERVAL_SECONDS = 0.5


def _worker_id() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _claim_or_find_daily_bite(db: Session, user_id: str, item_id: str, today: date_cls, worker_id: str):
    """Try to become the sole generator for (user_id, item_id, today).

    Returns (bite_row, claimed: bool):
      · claimed=True  → this worker owns a fresh placeholder row (cards is
        NULL) and must generate, then finalize or release it.
      · claimed=False → someone else already has (or had) this slot.
        `bite_row` is that row — either a completed session (cards set,
        caller should just return it) or another worker's live/expired
        claim (caller should poll or re-attempt).

    Uses the SAME unique index (uq_daily_bites_user_item_date) this
    function's own IntegrityError-catch-and-requery tail already relied on
    — inserting the claim placeholder is just an earlier use of that same
    constraint, now firing BEFORE any LLM call instead of after.
    """
    placeholder = DailyBite(
        id=str(uuid.uuid4()), user_id=user_id, library_item_id=item_id, date=today,
        title="", insight="", reflection="", action="",
        claimed_by=worker_id, claimed_until=datetime.utcnow() + timedelta(minutes=GENERATION_CLAIM_MINUTES),
    )
    db.add(placeholder)
    try:
        db.commit()
        db.refresh(placeholder)
        return placeholder, True
    except IntegrityError:
        db.rollback()

    # Someone already has a row for this slot — try to take over an EXPIRED
    # lease on an incomplete row (cards still NULL). An atomic conditional
    # UPDATE, exactly like _claim_personalization_row: the predicate is
    # evaluated by the database against committed state, never an
    # inspect-then-write on a loaded ORM object.
    now = datetime.utcnow()
    result = db.execute(
        update(DailyBite)
        .where(
            DailyBite.user_id == user_id,
            DailyBite.library_item_id == item_id,
            DailyBite.date == today,
            DailyBite.cards.is_(None),
            (DailyBite.claimed_until.is_(None)) | (DailyBite.claimed_until < now),
        )
        .values(claimed_by=worker_id, claimed_until=now + timedelta(minutes=GENERATION_CLAIM_MINUTES))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount == 1:
        row = (
            db.query(DailyBite)
            .filter(
                DailyBite.user_id == user_id,
                DailyBite.library_item_id == item_id,
                DailyBite.date == today,
            )
            .first()
        )
        return row, True

    # Not claimable by us right now — read whatever is there (complete row,
    # or someone else's live claim) for the caller to act on.
    row = (
        db.query(DailyBite)
        .filter(
            DailyBite.user_id == user_id,
            DailyBite.library_item_id == item_id,
            DailyBite.date == today,
        )
        .first()
    )
    return row, False


def _finalize_daily_bite_claim(db: Session, bite_id: str, worker_id: str, fields: dict) -> bool:
    """Write the generated content — only if `worker_id` still owns the
    claim. Mirrors bites.py's _finalize_personalization_answer exactly: an
    atomic `UPDATE ... WHERE id = :id AND claimed_by = :worker_id`, rowcount
    checked, never a blind write. Returns False if superseded (lease
    expired and someone else took over) — the caller must not trust its own
    generated content as canonical in that case."""
    values = dict(fields)
    values["claimed_by"] = None
    values["claimed_until"] = None
    result = db.execute(
        update(DailyBite)
        .where(DailyBite.id == bite_id, DailyBite.claimed_by == worker_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _release_daily_bite_claim(db: Session, bite_id: str, worker_id: str) -> None:
    """On generation failure: delete the placeholder row — but ONLY if
    `worker_id` still owns it (a superseded worker's failure must not touch
    the row the legitimate new owner is now working on). Deleting rather
    than resetting to a 'pending' state is deliberate: this model has no
    separate status field, and DELETE immediately frees the unique index
    slot so the very next request's INSERT-based claim attempt succeeds
    without needing to understand any intermediate state.

    Rolls back first: this is called from an except block, and the
    generation logic it is unwinding may have left uncommitted ORM-level
    changes (e.g. story mode's `item.story_progress` mutation) or, if the
    failure was itself a raw DB error, a transaction that must be rolled
    back before this session can execute anything else."""
    db.rollback()
    db.execute(
        DailyBite.__table__.delete().where(
            DailyBite.id == bite_id, DailyBite.claimed_by == worker_id,
        )
    )
    db.commit()


def _renew_daily_bite_claim(db: Session, bite_id: str, worker_id: str) -> bool:
    """Extend this worker's generation lease while it is still working.

    Re-audit finding #5-4 (Sep 2026): the fixed GENERATION_CLAIM_MINUTES
    lease had nothing renewing it. Story metadata, wisdom generation, and an
    optional personalization-question call first can together legitimately
    exceed the lease window on a slow provider — at which point a retried
    request (or the scheduler re-attempting the same slot) could steal the
    "expired" lease and start a SECOND real generation while the first was
    still genuinely in flight, exactly the double-LLM-call cost this whole
    mechanism exists to prevent. Same shape as bites.py's
    _renew_personalization_claim / connect.py's chat-turn renewal: an atomic
    `UPDATE ... WHERE id = :id AND claimed_by = :worker_id`, rowcount
    checked — renewal is ownership-conditional, so a worker that has already
    been superseded simply stops renewing rather than fighting the new
    owner.
    """
    result = db.execute(
        update(DailyBite)
        .where(DailyBite.id == bite_id, DailyBite.claimed_by == worker_id)
        .values(claimed_until=datetime.utcnow() + timedelta(minutes=GENERATION_CLAIM_MINUTES))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


_GENERATION_HEARTBEAT_SECONDS = 60.0


class _GenerationHeartbeat:
    """Renews this worker's DailyBite generation lease while it is blocked
    in one or more LLM calls (re-audit finding #5-4).

    Same design as bites.py's _PersonalizeHeartbeat / connect.py's
    _ChatTurnHeartbeat: a background daemon thread with its OWN SessionLocal
    (the caller's session is idle on another stack while blocked in network
    I/O and must not be shared across threads), renewing on a fixed
    interval well under the lease window, stopping silently the moment it
    is no longer the owner (superseded) or `stop()` is called.
    """

    def __init__(self, bite_id: str, worker_id: str, interval=None):
        self.bite_id = bite_id
        self.worker_id = worker_id
        self.interval = interval if interval is not None else _GENERATION_HEARTBEAT_SECONDS
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        while not self._stop.wait(self.interval):
            db = SessionLocal()
            try:
                if not _renew_daily_bite_claim(db, self.bite_id, self.worker_id):
                    return   # no longer the owner — stop renewing
            except Exception:
                logger.exception("Generation heartbeat failed for bite %s", self.bite_id)
            finally:
                db.close()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _wait_for_daily_bite(db: Session, user_id: str, item_id: str, today: date_cls) -> Optional[DailyBite]:
    """Bounded synchronous poll for the OTHER worker's row to finish
    generating. This is a real HTTP request, so the wait is capped
    (GENERATION_POLL_TIMEOUT_SECONDS) — a caller that gives up sees a
    retryable error, never an indefinite hang. `db.expire_all()` on every
    poll matches _read_personalization_row's discipline: without it,
    SQLAlchemy's identity map would keep handing back the same
    cards-is-None snapshot this session loaded on its first read, hiding
    the other worker's committed finalize."""
    deadline = time.monotonic() + GENERATION_POLL_TIMEOUT_SECONDS
    while True:
        db.expire_all()
        row = (
            db.query(DailyBite)
            .filter(
                DailyBite.user_id == user_id,
                DailyBite.library_item_id == item_id,
                DailyBite.date == today,
            )
            .first()
        )
        if row is None:
            # The owner released (failed) its claim — the slot is free again.
            return None
        if row.cards:
            return row
        if time.monotonic() >= deadline:
            return row  # still generating — caller decides how to respond
        time.sleep(GENERATION_POLL_INTERVAL_SECONDS)

# read length → total cards in the deck / retrieval breadth / story words
CARD_TARGETS = {5: 5, 10: 8, 15: 12}
WISDOM_TOP_K = {5: 6, 10: 10, 15: 14}
STORY_WORDS = {5: 1100, 10: 2200, 15: 3300}

# ── Dynamic growth-profile personalization (Aug 2026) ───────────────────────
# Chance any single ELIGIBLE wisdom session also asks a grounded
# personalization question. Deliberately unseeded/probabilistic per session
# (not a fixed-N counter, not adaptive to profile confidence) — the founder's
# own framing was "every once in a while", and replay safety comes from the
# surrounding DailyBite per-day cache (this function is only ever reached
# once per (user, item, day) — see generate_session_for_item's docstring),
# not from the roll itself being deterministic.
PERSONALIZATION_PROBABILITY = 0.20
# Below this, the retrieved excerpts are too thin to ground a genuine
# preference question in the book's actual content, not just its topic.
PERSONALIZATION_MIN_CHUNK_CHARS = 400
PERSONALIZATION_MIN_CHUNKS = 3


def _roll_personalization(db: Session, user: User, item: LibraryItem, chunks: List[str]) -> bool:
    """Whether THIS wisdom session should also carry a personalization card.

    Called once per (user, item, day) from inside generate_session_for_item
    — the SAME function both the on-demand HTTP path and the scheduler's
    pre-generation path share, so scheduler-delivered nibbles get this
    feature too, not just on-demand taps. Must be called BEFORE card_target
    is finalized (the extra card has to be baked into the exact card count
    the schema enforces, not appended after generation)."""
    if len(chunks) < PERSONALIZATION_MIN_CHUNKS:
        return False
    if sum(len(c) for c in chunks) < PERSONALIZATION_MIN_CHUNK_CHARS:
        return False
    # Not the user's first session with this book — a specific, book-grounded
    # question reads better once the user has actually started the book, and
    # chunk_ids' progressive-coverage exclusion means session 2+ retrieves a
    # more book-specific slice than session 1's cold-start query.
    prior_exists = (
        db.query(DailyBite.id)
        .filter(DailyBite.user_id == user.id, DailyBite.library_item_id == item.id)
        .first()
        is not None
    )
    if not prior_exists:
        return False
    return random.random() < PERSONALIZATION_PROBABILITY


def _insert_personalization_card(result: dict, question: dict, profile_id=None) -> None:
    """Splice the already-validated personalization card into a generated
    deck, in place, immediately before the summary card.

    Server-side insertion is deliberate — see the long note at the call
    site. The card object placed here carries the SAME option list (ids
    included) that gets persisted on the PersonalizationQuestion row, so
    "opt0" on screen and "opt0" in the database are the same option by
    construction rather than by trusting a model to preserve order.

    `profile_id` is the growth profile this session was generated FOR, and
    it must ride on the card (re-audit finding #1). The first fix persisted
    it on the database row but never put it on the card, so the app read
    `card.profileId === undefined` and silently fell back to whatever
    profile happened to be active at answer time — the exact bug the fix
    was supposed to close, still fully reachable on the common
    multiple-choice path.

    Never raises: personalization is an occasional bonus card, and a
    malformed deck shape must degrade to an ordinary (still perfectly
    valid) session rather than cost the user their nibble.
    """
    cards = result.get("cards")
    if not isinstance(cards, list) or not cards:
        return
    card = {
        "kind": "personalize",
        "eyebrow": question.get("eyebrow") or "ONE QUICK QUESTION",
        "title": question.get("question") or "",
        "body": None,
        "highlight": question.get("highlight"),
        "options": None,
        "explanation": None,
        "personalizeOptions": question.get("options") or [],
        "profileId": profile_id,
    }
    if not card["title"] or not card["personalizeOptions"]:
        return
    # Before the summary when there is one (the normal case — validate_wisdom
    # guarantees it); otherwise append, so an unexpected deck shape still
    # yields a coherent deck rather than a card in a nonsensical position.
    if cards[-1].get("kind") == "summary":
        cards.insert(len(cards) - 1, card)
    else:
        cards.append(card)


class SessionGenerationError(Exception):
    """A session couldn't be generated (bad input, retrieval empty, provider
    failure) — or, since the claim/lease fix (finding #5, Aug 2026), a
    retryable "someone else is already generating this" signal.

    `code`, when set, lets a caller distinguish a genuinely retryable
    condition (e.g. 'session_generating') from an ordinary failure, the
    same shape bites.py's personalize-answer endpoint already uses for its
    'personalize_processing' 409 — a structured `{code, message}` detail
    rather than a bare string, so a client can branch on `code` without
    parsing prose."""

    def __init__(self, message: str, status_code: int = 502, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _slice_words(text: str, start: int, count: int) -> str:
    """The slice of `text` covering words [start, start+count) — with every
    space, line break and blank line between them left exactly as written.

    Story progress is stored as a word offset, and the old implementation
    resolved it with `" ".join(text.split()[a:b])`, which threw away every
    paragraph break in the book before the text was ever shown to the reader.
    Word offsets stay valid here: `\\S+` tokenises identically to `str.split()`.
    """
    spans = [m.span() for m in re.finditer(r"\S+", text)]
    if start >= len(spans):
        return ""
    end = min(start + count, len(spans))
    return text[spans[start][0]:spans[end - 1][1]]


def _paragraphs(text: str) -> List[str]:
    """Blank-line-separated paragraphs, each with its internal line breaks
    (dialogue, verse, lists) intact."""
    return [p.strip("\n") for p in re.split(r"\n[ \t]*\n", text) if p.strip()]


def split_story_cards(excerpt: str, card_target: int) -> List[str]:
    """Cut today's portion into `card_target` card bodies at paragraph
    boundaries, balanced by word count. Purely mechanical — no model touches
    the text, so what the reader sees is byte-for-byte the author's."""
    paras = _paragraphs(excerpt)
    if not paras:
        return []
    if len(paras) <= card_target:
        return paras

    counts = [len(p.split()) for p in paras]
    per_card = sum(counts) / card_target
    cards: List[str] = []
    cur: List[str] = []
    cur_words = 0
    for i, para in enumerate(paras):
        cur.append(para)
        cur_words += counts[i]
        remaining_paras = len(paras) - i - 1
        remaining_cards = card_target - len(cards) - 1
        # Close this card once it has its share — unless the paragraphs left
        # are only just enough to fill the cards left.
        if remaining_cards > 0 and (
            cur_words >= per_card or remaining_paras <= remaining_cards
        ):
            cards.append("\n\n".join(cur))
            cur, cur_words = [], 0
    if cur:
        cards.append("\n\n".join(cur))
    return cards


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
    the DailyBite. If a concurrent write already created — or is currently
    generating — the same (user, item, date) row, this NEVER calls the LLM a
    second time (finding #5, Aug 2026): see DailyBite.claimed_by's docstring
    in app/models/bite.py.

    This is the ONE place both callers converge — the on-demand HTTP handler
    (bites.py's get_or_create_session) and the scheduler's DeliveryCycle-
    claimed generation phase (delivery_lifecycle.py's process_generation_phase)
    both call this function directly, so claiming here (rather than in
    either caller) covers an HTTP request racing the scheduler for the same
    slot just as well as it covers two HTTP requests racing each other.

    Sequence:
      1. Try to INSERT a placeholder row (cards=NULL) under
         uq_daily_bites_user_item_date. Win → generate. Lose (IntegrityError)
         → try to take over an EXPIRED lease on the existing incomplete row
         (a crashed/hung worker's claim). Still lose → someone else holds a
         LIVE claim, or the row is already complete.
      2. The WINNER generates (unchanged generation logic below) and
         finalizes the placeholder via an atomic claimed_by-checked UPDATE —
         never a blind write, so a lease that expired mid-generation and was
         reclaimed by someone else is refused rather than silently
         overwriting the new owner's work. Generation failure releases
         (deletes) the placeholder so the very next request can claim fresh.
      3. The LOSER never touches the LLM. If the row is already complete, it
         is returned immediately. Otherwise the loser polls briefly
         (GENERATION_POLL_TIMEOUT_SECONDS) for the winner to finish; if the
         winner finishes in time, that row is returned. If not — or if the
         claim was released (winner failed) and no one has re-claimed it
         yet — a retryable SessionGenerationError (409, code
         'session_generating') is raised, matching this codebase's existing
         retry-on-409 shape (bites.py's personalize_processing).

    Does NOT enforce daily caps — callers own that pre-check (the HTTP
    handler and the scheduler differ there); the claim above is orthogonal
    to the cap and only prevents a DUPLICATE generation for the same slot.
    """
    if not is_source_unlocked(user, item):
        # Belt-and-suspenders: both callers (the HTTP handler and the
        # scheduler) already filter locked sources out before reaching here,
        # but this is the ONE place that actually calls the paid LLM, so it
        # is also the one place that must refuse unconditionally rather than
        # trust every caller to have checked (Task 2).
        raise SessionGenerationError("This source is locked for Free accounts.", status_code=403)

    worker_id = _worker_id()
    claimed_row, won = _claim_or_find_daily_bite(db, user.id, item.id, today, worker_id)

    if not won:
        if claimed_row is not None and claimed_row.cards:
            return claimed_row  # already complete — the common "existing" case
        # Either a live claim, or the row was just released (generation
        # failed elsewhere) and nobody has re-claimed it yet. Poll briefly
        # rather than calling the LLM ourselves — this IS the fix: the
        # loser must never reach generation.
        finished = _wait_for_daily_bite(db, user.id, item.id, today)
        if finished is not None and finished.cards:
            return finished
        raise SessionGenerationError(
            "Nibbler is already writing this one — try again in a moment.",
            status_code=409, code="session_generating",
        )

    # We won the claim: `claimed_row` is OUR placeholder (cards is NULL).
    # Generate into it; finalize on success, release (delete) on failure —
    # never leave a dangling claimed-but-broken row blocking the slot.
    #
    # Re-audit finding #5-4: a heartbeat renews the lease for the whole
    # duration of generation, so a slow provider (retries, a fallback chain,
    # an optional personalization-question call first) can never outlive
    # GENERATION_CLAIM_MINUTES while this worker is still genuinely working
    # — closing the takeover-during-active-generation gap. Started before
    # the call and stopped in `finally` so it never outlives this attempt
    # either way (success, failure, or an unexpected exception).
    heartbeat = _GenerationHeartbeat(claimed_row.id, worker_id)
    heartbeat.start()
    try:
        bite = _build_session_content(
            db, user=user, item=item, read_length=read_length,
            profile=profile, today=today, origin=origin,
            claimed_bite_id=claimed_row.id, worker_id=worker_id,
        )
    except BaseException:
        _release_daily_bite_claim(db, claimed_row.id, worker_id)
        raise
    finally:
        heartbeat.stop()
    if bite is None:
        # Finalize reported we no longer own the claim (an expired lease was
        # taken over mid-generation) — our work is discarded, and whichever
        # worker legitimately owns the slot now is canonical. Behave exactly
        # like a loser: wait briefly, then return whatever is there.
        finished = _wait_for_daily_bite(db, user.id, item.id, today)
        if finished is not None and finished.cards:
            return finished
        raise SessionGenerationError(
            "Nibbler is already writing this one — try again in a moment.",
            status_code=409, code="session_generating",
        )
    return bite


def _build_session_content(
    db: Session,
    *,
    user: User,
    item: LibraryItem,
    read_length: int,
    profile: dict,
    today: date_cls,
    origin: str,
    claimed_bite_id: str,
    worker_id: str,
) -> Optional[DailyBite]:
    """The actual generation logic (unchanged from before the claim/lease
    fix) — runs ONLY for the worker that won the claim in
    generate_session_for_item. Writes its result into the already-claimed
    placeholder row (`claimed_bite_id`) via an ownership-checked UPDATE
    rather than inserting a new row, since the placeholder already holds
    the unique (user, item, date) slot. Returns the finalized DailyBite, or
    None if the claim was lost before finalize could run (caller treats
    that exactly like a loser)."""
    read_length = read_length if read_length in CARD_TARGETS else 5
    # Which model generates this deck is a routing decision, not a tier one:
    # free, trial and premium users all get whatever LLM_ROUTING_MODE selects.
    llm = LLMService()
    mode = item.mode or "wisdom"
    card_target = CARD_TARGETS[read_length]
    story_finished = False
    goal_passage = None
    chunk_ids = None

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
            excerpt = _slice_words(item.content or "", progress, n)
            part_number = progress // n + 1
            bodies = split_story_cards(excerpt, max(3, card_target - 1))
            if not bodies:
                raise SessionGenerationError("No readable text stored for this book.", 422)
            # Figures the reader has already reached. A picture from further
            # ahead is a spoiler — a character, a place, a plot beat they have
            # not met — so the shortlist is capped at today's position and the
            # same cap is re-applied after the model answers.
            # Both sides of this comparison are WORD fractions of the same
            # text: `progress` is a word offset into item.content, and a
            # candidate's position is the fraction of the book's words before
            # it. Candidates recorded in pages or spine units are refused
            # outright rather than converted — see image_select.
            story_max_position = min(1.0, (progress + n) / max(1, len(words)))
            story_candidates = image_select.safe_shortlist(
                item.images, excerpt, max_position=story_max_position,
            )

            # The model never carries the prose — it only names what it reads.
            # Story mode's whole promise is the book itself, so a paraphrase or
            # a silently reflowed paragraph is a bug, not a style choice.
            try:
                meta = llm.generate_story_metadata(
                    book_title=item.title, author=item.author,
                    card_bodies=bodies, part_number=part_number,
                    image_options=image_select.safe_prompt(story_candidates),
                )
            except Exception as e:
                logger.warning("Story metadata failed (%s) — serving plain headings", e)
                meta = {}
            headings = meta.get("headings") or []
            story_image_ids = meta.get("imageIds") or []
            result = {
                "title": meta.get("title") or f"{item.title} — part {part_number}",
                "chapter": f"PART {part_number}",
                "headline": meta.get("headline") or "Today's portion of your book.",
                "preview": meta.get("preview") or "",
                "cards": [
                    {
                        "kind": "story",
                        "eyebrow": "TODAY'S READING" if i == 0 else "THE STORY CONTINUES",
                        "title": headings[i] if i < len(headings) else "",
                        "body": body,
                        # Positional: story cards are server-owned and have no
                        # id the model could name, so it answers with an array
                        # parallel to `headings`. Validated below.
                        "imageId": (story_image_ids[i]
                                    if i < len(story_image_ids) else None),
                    }
                    for i, body in enumerate(bodies)
                ],
                "quiz": None,
            }
            # Failure here must never cost the reader their portion: the text
            # is already correct and complete, and a picture is a garnish.
            try:
                image_select.attach_images(
                    result["cards"], shortlisted=story_candidates,
                    user_id=user.id, item_id=item.id,
                    max_position=story_max_position,
                )
            except Exception as e:
                logger.warning("Story image attach failed (%s) — text-only portion", e)
                for card in result["cards"]:
                    card.pop("imageId", None)
            item.story_progress = min(progress + n, len(words))
    else:
        profile = profile or {}
        pq = _profile_query(profile)
        query = pq or item.title
        embeddings = EmbeddingService()

        # Progressive coverage: exclude every chunk this user's previous
        # sessions already drew from, so each nibble explores NEW ground —
        # without this, the same profile query returned the same top-K chunks
        # every single day, and 'Explored %' could never honestly grow.
        served: set = set()
        for (ids,) in (
            db.query(DailyBite.chunk_ids)
            .filter(
                DailyBite.user_id == user.id,
                DailyBite.library_item_id == item.id,
                DailyBite.chunk_ids.isnot(None),
            )
            .all()
        ):
            served.update(i for i in (ids or []) if isinstance(i, int))

        try:
            fresh = embeddings.search_item_fresh(
                query=query, user_id=user.id, item_id=item.id,
                top_k=WISDOM_TOP_K[read_length],
                exclude_indexes=sorted(served),
            )
        except Exception as e:
            logger.warning("Fresh retrieval failed (%s) — raw-text fallback", e)
            fresh = []
        chunks = [f["text"] for f in fresh if f.get("text")]
        chunk_ids = [f["chunk_index"] for f in fresh if isinstance(f.get("chunk_index"), int)]
        # Retrieval is ranked by similarity to the growth profile, so the top
        # chunk IS today's most goal-relevant passage (Connect tab uses it).
        if chunks and pq:
            goal_passage = " ".join(chunks[0].split())
            goal_passage = goal_passage[:280] + ("…" if len(goal_passage) > 280 else "")
        if not chunks and item.content:
            chunks = [item.content[:8000]]  # Pinecone down — fall back to raw text
            chunk_ids = []
        if not chunks:
            raise SessionGenerationError("No indexed content found for this item.", 422)
        # Figures whose caption/alt/nearby text overlaps the passages this
        # deck is being written from. Empty for most books, which is the
        # expected outcome — a text-only deck is a correct deck.
        wisdom_candidates = image_select.safe_shortlist(item.images, " ".join(chunks))

        # Dynamic growth-profile personalization (Aug 2026): decided BEFORE
        # card_target is finalized, and generated in its own call BEFORE the
        # deck call, so its grounding is validated once against these same
        # excerpts (see llm.generate_personalization_question) rather than
        # trusted to the deck model re-deriving it faithfully. `chunk_ids`
        # here excludes the raw-text fallback (chunk_ids == [] in that case),
        # which is correct — a personalization question should only ever be
        # grounded in real retrieved passages, never the 8000-char fallback.
        personalization_question = None
        if chunk_ids and _roll_personalization(db, user, item, chunks):
            personalization_question = llm.generate_personalization_question(
                book_title=item.title, author=item.author,
                profile=profile, context_chunks=chunks,
            )
            if personalization_question:
                # Stamp a stable, purely positional id onto each option.
                # "optN" by array index — never something a model invents
                # (personalization_option_schema has no id field at all).
                # This exact list, ids included, is BOTH persisted on the
                # PersonalizationQuestion row below AND spliced verbatim into
                # the deck after generation, so the id the user taps and the
                # id the answer endpoint resolves are the same object.
                for i, opt in enumerate(personalization_question.get("options") or []):
                    opt["id"] = f"opt{i}"

        try:
            # NOTE (audit fix, Aug 2026): the deck is generated at its NORMAL
            # card count and is told nothing about personalization. The card
            # is inserted server-side, below.
            #
            # The original implementation asked the deck model to reproduce a
            # pre-generated personalize card verbatim at a pinned position,
            # then re-stamped optN ids onto whatever came back BY POSITION.
            # That silently assumed the model preserves option ORDER, and
            # nothing enforced it — validate_wisdom checked option
            # count/text/tag validity but never equality with the original —
            # so a model that reordered the options produced a card where
            # "opt0" on screen meant a different answer than "opt0" in the
            # persisted row. The user's tap then applied the wrong profile
            # tag: silent, plausible, and invisible from the outside.
            #
            # Splicing server-side removes the failure mode rather than
            # trying to detect it: the card the user sees IS the object that
            # was grounding-validated and persisted, not a model's copy.
            result = llm.generate_wisdom_session(
                book_title=item.title, author=item.author,
                profile=profile, context_chunks=chunks,
                card_target=card_target, read_length=read_length,
                image_options=image_select.safe_prompt(wisdom_candidates),
            )
        except Exception as e:
            raise SessionGenerationError(f"Session generation failed: {e}", 502)

        if personalization_question:
            _insert_personalization_card(
                result, personalization_question,
                profile_id=(profile or {}).get("id"),
            )

        # Ownership, book and shortlist membership are all re-checked here from
        # the stored rows — the id came back from a model, so nothing about it
        # is trusted. An exception must not lose an otherwise valid deck.
        try:
            image_select.attach_images(
                result.get("cards") or [], shortlisted=wisdom_candidates,
                user_id=user.id, item_id=item.id,
            )
        except Exception as e:
            logger.warning("Wisdom image attach failed (%s) — text-only deck", e)
            for card in result.get("cards") or []:
                if isinstance(card, dict):
                    card.pop("imageId", None)

    # Finalize the ALREADY-CLAIMED placeholder row (claimed_bite_id) rather
    # than inserting a new one — it already holds the unique (user, item,
    # date) slot (see generate_session_for_item's claim step above), so
    # there is no more IntegrityError race to handle here. What CAN still
    # happen is losing the lease mid-generation (a slow provider chain
    # outlasting GENERATION_CLAIM_MINUTES, reclaimed by a later request) —
    # `_finalize_daily_bite_claim` is the same ownership-checked atomic
    # UPDATE idiom as bites.py's `_finalize_personalization_answer`, and a
    # False return means exactly that: our content is discarded, the caller
    # falls back to the loser path (poll/return the new owner's result)
    # rather than trusting content generated under a lease we no longer hold.
    finalize_fields = dict(
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
        chunk_ids=chunk_ids,
        origin=origin,
    )
    if not _finalize_daily_bite_claim(db, claimed_bite_id, worker_id, finalize_fields):
        # Superseded: discard EVERYTHING this attempt did in-session,
        # including story mode's `item.story_progress` mutation above (real
        # state, not just this row's content) — it must never be committed
        # by a worker that lost the claim. generate_session_for_item treats
        # this exactly like a loser (poll/return the new owner's result).
        db.rollback()
        return None

    # Story mode's progress advance is real STATE (not just this row's
    # content) and must only ever be committed for the worker that actually
    # won and finalized — the claim boundary above is what makes that true
    # even though `item.story_progress` was mutated earlier in this
    # function's story branch, before finalize was known to succeed.
    if mode == "story":
        db.commit()

    # Written AFTER finalize succeeds, in its own transaction — the deck
    # referencing it (via daily_bite_id) is now durably persisted, so this
    # can never create a question row against a bite we didn't actually win.
    # `daily_bite_id` is unique (PersonalizationQuestion.__table_args__), so
    # this can never create two rows for one bite.
    if mode != "story" and personalization_question:
        db.add(PersonalizationQuestion(
            user_id=user.id,
            daily_bite_id=claimed_bite_id,
            library_item_id=item.id,
            profile_id=(profile or {}).get("id"),
            question=personalization_question.get("question") or "",
            options=personalization_question.get("options") or [],
            source_chunk_ids=chunk_ids,
        ))
        db.commit()

    # A session was actually generated for this source — real "use", the
    # authoritative signal the deterministic downgrade fallback ranks on
    # (see entitlement_service._fallback_candidates), distinct from a bare
    # metadata edit that also bumps `updated_at`.
    touch_last_active(item)
    db.commit()

    bite = db.query(DailyBite).filter(DailyBite.id == claimed_bite_id).first()
    return bite
