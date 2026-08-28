from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.middleware.auth import get_current_user
from app.rate_limit import limiter
from app.models.user import User
from app.models.bite import DailyBite, SavedBite
from app.models.library import LibraryItem
from app.models.personalization import PersonalizationQuestion
from app.schemas.bite import (
    BiteResponse, SavedBiteResponse, BiteHistoryResponse,
    SessionHistoryItem, SessionHistoryResponse,
)
from app.services import mixpanel_service
from app.services.llm import LLMService
from app.services.session_service import generate_session_for_item, SessionGenerationError, CARD_TARGETS
from app.services.entitlement_service import is_source_unlocked


def _filter_locked_sources(db: Session, user: User, rows: list):
    """Task 2 remediation (locked-source access audit): drop any row whose
    `library_item_id` points at a currently-LOCKED source. Rows with no
    `library_item_id` (legacy, pre-session bites) or whose item no longer
    exists are left visible — documented conservative policy: they predate
    per-source entitlement tracking entirely and cannot be attributed to any
    source's current lock state, so treating them as locked would hide
    content the account has always fully owned, for a restriction that did
    not exist when they were created. This is a deliberate default-allow for
    the unattributable case, not a silent "proven unlocked" claim — it is
    the same conservative default `GET /bites/sessions` and `GET
    /bites/daily` already apply."""
    item_ids = {r.library_item_id for r in rows if r.library_item_id}
    if not item_ids:
        return rows
    items_by_id = {
        i.id: i for i in db.query(LibraryItem).filter(LibraryItem.id.in_(item_ids)).all()
    }
    return [
        r for r in rows
        if not r.library_item_id
        or r.library_item_id not in items_by_id
        or is_source_unlocked(user, items_by_id[r.library_item_id])
    ]


def _bite_source_locked(db: Session, user: User, bite: DailyBite) -> bool:
    """True only when this bite is attributable to a specific owned source
    AND that source is currently locked — see `_filter_locked_sources` for
    why an unattributable (legacy) bite is never treated as locked."""
    if not bite.library_item_id:
        return False
    item = db.query(LibraryItem).filter(LibraryItem.id == bite.library_item_id).first()
    if not item:
        return False
    return not is_source_unlocked(user, item)
from app.config import get_settings
import uuid

router = APIRouter(prefix="/bites", tags=["bites"])
settings = get_settings()

# Fallback window for the free/premium generation cap, on the SERVER clock, used
# only when the user's timezone is unknown. Matches
# notification_service.NIBBLE_LOCK_HOURS so the tap path and the scheduler agree.
CAP_WINDOW_HOURS = 23


def _cap_window(user: User):
    """(window_start_utc, resets_at_utc) for this user's generation allowance.

    Counting must use the SERVER clock — `date` comes from the client, so
    bucketing on it let anyone claim tomorrow and get a fresh allowance. But a
    flat rolling window has its own problem: it turns "1 per day" into "1 per
    23 hours", so a free user who reads at 22:00 is refused at 09:00 the next
    morning — on a day they haven't used at all.

    `users.timezone` is recorded on every launch (PATCH /sync/identity), so when
    it's known the cap is bucketed by the user's real local day: unforgeable
    (still server-computed) *and* correct. Falls back to the rolling window when
    the timezone is missing or unrecognised.
    """
    now = datetime.utcnow()
    if user.timezone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(user.timezone)
            local_now = datetime.now(tz)
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            next_midnight = local_midnight + timedelta(days=1)
            return (
                local_midnight.astimezone(timezone.utc).replace(tzinfo=None),
                next_midnight.astimezone(timezone.utc).replace(tzinfo=None),
            )
        except Exception:
            pass   # unknown/invalid zone name, or no tzdata on the image
    return now - timedelta(hours=CAP_WINDOW_HOURS), now + timedelta(hours=CAP_WINDOW_HOURS)


def _cap_message(cap: int, resets_at: datetime) -> str:
    """Say when the allowance actually lifts.

    The old copy said "Come back tomorrow!" unconditionally, which was wrong
    under a rolling window — a user refused at 09:00 was being told to wait a
    day when the real answer was a few hours.
    """
    hours = max(1, round((resets_at - datetime.utcnow()).total_seconds() / 3600))
    when = "tomorrow" if hours >= 12 else f"in about {hours} hour{'s' if hours != 1 else ''}"
    unit = "bites" if cap > 1 else "bite"
    return f"You've used today's {cap} {unit}. Come back {when}."

# ── Per-book session generation (July 2026) ───────────────────────────────
# The generation logic lives in app/services/session_service.py (shared with
# the delivery-time scheduler). CARD_TARGETS is imported for read-length checks.


class SessionProfile(BaseModel):
    # The LOCAL growth-profile id (ProfileRepository.js) this session is for
    # — Aug 2026, additive. Threaded through to PersonalizationQuestion.
    # profile_id so an answer's deltas can be attributed to the profile that
    # was active at GENERATION time even if the user later switches profiles
    # before answering. Optional so an older app build (which omits it)
    # keeps working exactly as before.
    id: Optional[str] = None
    name: Optional[str] = None
    lifeArea: Optional[str] = None
    aspirationLabel: Optional[str] = None
    aspirationUnderstanding: Optional[str] = None
    confidenceStyle: Optional[str] = None
    goalOrientation: Optional[str] = None
    contentMode: Optional[str] = None
    interests: Optional[List[str]] = None


class SessionRequest(BaseModel):
    library_item_id: str
    read_length: int = 5
    growth_profile: Optional[SessionProfile] = None
    # The user's LOCAL date (YYYY-MM-DD) — the server day flips at UTC
    # midnight, which is mid-evening for the Americas. Accepted within ±1
    # day of the server date.
    client_date: Optional[str] = None


def _effective_today(client_date_str: Optional[str]) -> date:
    today = date.today()
    if not client_date_str:
        return today
    try:
        d = date.fromisoformat(client_date_str)
    except ValueError:
        return today
    return d if abs((d - today).days) <= 1 else today


class SessionResponse(BaseModel):
    id: str
    library_item_id: str
    date: date
    mode: str
    read_length: int
    title: str
    chapter: Optional[str] = None
    headline: Optional[str] = None
    preview: Optional[str] = None
    cards: list
    quiz: Optional[list] = None
    story_finished: bool = False
    goal_passage: Optional[str] = None  # today's most goal-relevant excerpt (wisdom only)


def _bite_to_session(bite: DailyBite) -> SessionResponse:
    return SessionResponse(
        id=bite.id,
        library_item_id=bite.library_item_id,
        date=bite.date,
        mode=bite.mode or "wisdom",
        read_length=bite.read_length or 5,
        title=bite.title,
        chapter=bite.chapter,
        headline=bite.headline,
        preview=bite.preview,
        cards=bite.cards or [],
        quiz=bite.quiz,
        story_finished=bool(bite.theme == "story_finished"),
        goal_passage=bite.goal_passage,
    )


@router.post("/session", response_model=SessionResponse)
# Generous safety net against a genuine runaway-loop bug, NOT the real cost
# control — that's the todays_generations/cap check below, which correctly
# counts only actual generations (persisted in the DB) and is what limits
# real Claude spend (1 free / 3 premium per day). This decorator used to be
# 30/day and counted EVERY call including free re-reads of an already-cached
# bite (the early return a few lines down) — a user simply re-opening the
# same 5 books a few times while testing could exhaust it with zero new
# generations, surfacing as "That bite got away" for no real reason.
@limiter.limit("200/day")
def get_or_create_session(
    request: Request,
    data: SessionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Today's card-deck session for one library item. Cached per (user, item, day)."""
    item = db.query(LibraryItem).filter(
        LibraryItem.id == data.library_item_id,
        LibraryItem.user_id == current_user.id,
        LibraryItem.deletion_state.is_(None),  # a tombstoned item is gone
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if not item.processed:
        raise HTTPException(status_code=409, detail="Nibbler is still reading this one — try again in a moment.")
    if not is_source_unlocked(current_user, item):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now. Upgrade to bring it back, or open one of your unlocked sources.",
            },
        )

    read_length = data.read_length if data.read_length in CARD_TARGETS else 5
    today = _effective_today(data.client_date)

    existing = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.library_item_id == item.id,
        DailyBite.date == today,
    ).first()
    if existing and existing.cards:
        return _bite_to_session(existing)
    # `force_new` used to live here: it hard-DELETED the existing row before
    # regenerating, which freed its own quota slot (so the cap could be spent
    # repeatedly), cascaded away any saved_bites row pointing at it, orphaned
    # notes/highlights that referenced the bite id, and for story mode skipped
    # content outright — story_progress had already advanced at generation time
    # and was never rewound. Nothing in the app ever sent it. Removed rather
    # than kept as a public field on an authenticated endpoint.

    # Daily generation caps (free 1 / premium 3, from config — previously
    # defined but never enforced). Re-opening today's existing sessions
    # returns above without counting.
    cap = (
        settings.premium_bites_per_day
        if current_user.effective_premium
        else settings.free_bites_per_day
    )
    # Counted over a rolling window of SERVER-CLOCK generated_at, not over the
    # row's `date`. `date` comes from the client (_effective_today accepts any
    # client_date within ±1 day, correctly, so the row is labelled in the
    # user's own timezone) — but counting on it meant a client sending
    # TOMORROW's date got a fresh, empty cap bucket. A free user could pull 3
    # real Claude generations per calendar day instead of 1, and a premium user
    # 9, just by lying about the date. generated_at is server-set and
    # unforgeable. 23h rather than 24h for the same reason the scheduler uses
    # NIBBLE_LOCK_HOURS: an unchanged daily cadence is ~24h apart, and a strict
    # 24h would block it on ordinary jitter.
    window_start, resets_at = _cap_window(current_user)
    todays_generations = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.generated_at >= window_start,
        # A deck that failed to generate shouldn't cost the user a slot.
        DailyBite.cards.isnot(None),
    ).count()
    if todays_generations >= cap:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "daily_limit_reached",
                "message": _cap_message(cap, resets_at),
                "limit": cap,
                "is_premium": current_user.effective_premium,
                "resets_at": resets_at.isoformat() + "Z",
            },
        )

    profile = (data.growth_profile.model_dump() if data.growth_profile else {}) or {}
    try:
        bite = generate_session_for_item(
            db, user=current_user, item=item,
            read_length=read_length, profile=profile,
            today=today, origin="manual",
        )
    except SessionGenerationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    background_tasks.add_task(mixpanel_service.track, "session_generated", current_user.id, {
        "mode": bite.mode, "read_length": bite.read_length, "cards": len(bite.cards or []),
    })
    return _bite_to_session(bite)


@router.get("/daily", response_model=List[SessionResponse])
def get_daily_nibbles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    The scheduler-prepared nibble set for Home (see NIBBLE_SESSION_LIFECYCLE.md):
    the currently-held UNREAD scheduled set if one exists, otherwise today's
    delivered set. Up to 3 (premium carousel) / 1 (free). Empty if nothing has
    been prepared yet (e.g. no active sources, or before the first delivery).
    """
    today = date.today()
    rows = (
        db.query(DailyBite)
        .filter(
            DailyBite.user_id == current_user.id,
            # Origin-AGNOSTIC, matching the scheduler's hold rule. This used to
            # require origin == "scheduled", which disagreed with
            # notification_service._live_unread_query: an unread MANUAL or
            # prefetched session held all future generation while never being
            # returned here, so Home couldn't show the very row that was
            # blocking it and the user had no way to clear the hold.
            or_(DailyBite.read_at.is_(None), DailyBite.date == today),
        )
        .order_by(DailyBite.date.desc(), DailyBite.generated_at.asc())
        .all()
    )
    if not rows:
        return []
    # Task 2: a session belonging to a currently-LOCKED source must not
    # surface on Home either — same rule GET /bites/sessions (Review)
    # already applies.
    rows = _filter_locked_sources(db, current_user, rows)
    if not rows:
        return []
    # Return only the most-recent scheduled date's set (held-unread wins over today).
    top_date = rows[0].date
    return [_bite_to_session(b) for b in rows if b.date == top_date]


@router.post("/{bite_id}/read")
def mark_bite_read(
    bite_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Mark a nibble as read — this releases the hold so the scheduler may prepare
    the next set at the next delivery time. Idempotent. Streak credit stays with
    POST /streak/checkin (called by the app on completion).
    """
    bite = db.query(DailyBite).filter(
        DailyBite.id == bite_id,
        DailyBite.user_id == current_user.id,
    ).first()
    if not bite:
        raise HTTPException(status_code=404, detail="Nibble not found.")
    if _bite_source_locked(db, current_user, bite):
        # Task 2 remediation: a mutation tied to a locked source's content
        # must be refused the same as reading it, so a direct API call
        # cannot reach protected source-derived content by a side door.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now.",
            },
        )
    if bite.read_at is None:
        bite.read_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "read_at": bite.read_at}


# ── Dynamic growth-profile personalization (Aug 2026) ───────────────────────

class PersonalizeAnswerRequest(BaseModel):
    # Mutually exclusive: a listed option's id, OR free text for the
    # always-present "something else" affordance the app renders client-side
    # (never LLM-authored — see schemas.py's personalization_option_schema
    # docstring).
    option_id: Optional[str] = None
    free_text: Optional[str] = None


class PersonalizeAnswerResponse(BaseModel):
    tags: List[str]
    interpreted_summary: Optional[str] = None


@router.post("/{bite_id}/personalize-answer", response_model=PersonalizeAnswerResponse)
@limiter.limit("30/hour")
def submit_personalize_answer(
    request: Request,
    bite_id: str,
    data: PersonalizeAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record the user's answer to a session's personalization card and
    return the resolved tags for the app to apply locally via
    ProfileRepository.recordEvent (see profileEvents.js's
    'personalization_answered' case) — this endpoint never touches
    Profile.growth_state itself, so there is exactly one write path for the
    growth profile (the app's own existing debounced PUT /profile/growth
    push), never a race between two direct writers.

    Idempotent by construction: the PersonalizationQuestion row was created
    at GENERATION time (session_service._roll_personalization), not here, so
    a second submit against an already-'answered' row (a retry, or a second
    device racing the first) returns the SAME resolved tags rather than
    re-applying or re-interpreting anything.
    """
    row = db.query(PersonalizationQuestion).filter(
        PersonalizationQuestion.daily_bite_id == bite_id,
        PersonalizationQuestion.user_id == current_user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="No personalization question on this session.")

    if row.status == "answered":
        # Idempotent replay — not an error. Mirrors update_growth_state's own
        # "a redundant write is a no-op, not a 409" convention.
        return PersonalizeAnswerResponse(
            tags=row.applied_tags or [], interpreted_summary=row.interpreted_summary,
        )

    bite = db.query(DailyBite).filter(DailyBite.id == bite_id, DailyBite.user_id == current_user.id).first()
    if bite and _bite_source_locked(db, current_user, bite):
        raise HTTPException(
            status_code=403,
            detail={"code": "source_locked", "message": "This source is Premium-only right now."},
        )

    free_text = (data.free_text or "").strip()
    interpreted_summary = None

    if data.option_id:
        options = row.options or []
        matched = next((o for o in options if o.get("id") == data.option_id or o.get("text") == data.option_id), None)
        tags = [matched["tag"]] if matched and matched.get("tag") else []
        row.answer_option_id = data.option_id
    elif free_text:
        llm = LLMService()
        interpreted = llm.interpret_personalization_answer(
            question=row.question, options=row.options or [], free_text=free_text,
        )
        tags = interpreted.get("tags") or []
        interpreted_summary = interpreted.get("summary")
        row.answer_free_text = free_text
    else:
        raise HTTPException(status_code=422, detail="Provide either option_id or free_text.")

    row.applied_tags = tags
    row.interpreted_summary = interpreted_summary
    row.status = "answered"
    row.answered_at = datetime.utcnow()
    db.commit()

    return PersonalizeAnswerResponse(tags=tags, interpreted_summary=interpreted_summary)


def _bite_to_response(bite: DailyBite, saved_ids: set) -> BiteResponse:
    return BiteResponse(
        id=bite.id,
        title=bite.title,
        insight=bite.insight,
        reflection=bite.reflection,
        action=bite.action,
        source=bite.source,
        theme=bite.theme,
        date=bite.date,
        is_saved=bite.id in saved_ids,
    )


# NOTE (July 2026): the legacy GET /bites/today endpoint was retired here.
# It required the retired chat-interview Profile row (so it 400'd for every
# local-onboarded user), the app has no callers, and its background streak
# update double-counted total_bites_read alongside POST /streak/checkin —
# which is now the single streak write path.


@router.get("/history", response_model=BiteHistoryResponse)
def get_bite_history(
    limit: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get past daily bites. Free users: last 7 days. Premium: full archive."""
    query = db.query(DailyBite).filter(DailyBite.user_id == current_user.id)

    if not current_user.effective_premium:
        from datetime import timedelta
        cutoff = date.today() - timedelta(days=7)
        query = query.filter(DailyBite.date >= cutoff)

    bites = query.order_by(DailyBite.date.desc()).limit(limit).all()
    # Task 2 remediation: the insight/reflection/action text on a bite IS
    # source-derived content — history must not surface it once its source
    # has locked, exactly like Review and the daily set already refuse to.
    bites = _filter_locked_sources(db, current_user, bites)
    saved_ids = {s.bite_id for s in db.query(SavedBite).filter(SavedBite.user_id == current_user.id).all()}

    return BiteHistoryResponse(
        bites=[_bite_to_response(b, saved_ids) for b in bites],
        total=len(bites),
    )


@router.get("/sessions", response_model=SessionHistoryResponse)
@limiter.limit("60/hour")
def get_session_history(
    request: Request,
    limit: int = 60,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Past sessions WITH their full card decks and quizzes.

    This is what makes the Review tab survive a reinstall. `ReviewScreen`
    builds its flashcards exclusively from the local `SESSION_CACHE`, which
    keeps 7 days and is deliberately not synced — on the grounds that it
    "rebuilds from daily_bites on demand". Nothing rebuilt it: `GET
    /bites/history` returns the legacy `BiteResponse` shape, which has no
    `cards` and no `quiz`, and nothing in the app called it anyway. So every
    quiz stayed safely in PostgreSQL with no path back to the screen.

    Free users keep the documented 7-day history window; premium gets the
    archive, newest first.
    """
    query = db.query(DailyBite).filter(DailyBite.user_id == current_user.id)

    if not current_user.effective_premium:
        cutoff = date.today() - timedelta(days=7)
        query = query.filter(DailyBite.date >= cutoff)

    rows = (
        query.order_by(DailyBite.date.desc(), DailyBite.generated_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    # Decks with no cards are useless to the client and only cost bandwidth.
    rows = [r for r in rows if r.cards]

    # Task 2: a session belonging to a currently-LOCKED source must not
    # surface in Review.
    rows = _filter_locked_sources(db, current_user, rows)

    return SessionHistoryResponse(sessions=rows, total=len(rows))


@router.post("/{bite_id}/save", response_model=dict)
def save_bite(
    bite_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bite = db.query(DailyBite).filter(
        DailyBite.id == bite_id,
        DailyBite.user_id == current_user.id,
    ).first()
    if not bite:
        raise HTTPException(status_code=404, detail="Bite not found.")
    if _bite_source_locked(db, current_user, bite):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now.",
            },
        )

    existing = db.query(SavedBite).filter(
        SavedBite.bite_id == bite_id,
        SavedBite.user_id == current_user.id,
    ).first()
    if existing:
        return {"message": "Already saved"}

    saved = SavedBite(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        bite_id=bite_id,
    )
    db.add(saved)
    try:
        db.commit()
    except IntegrityError:
        # Unique index on (user_id, bite_id): a concurrent save won the race.
        db.rollback()
        return {"message": "Already saved"}
    return {"message": "Saved"}


@router.delete("/{bite_id}/save")
def unsave_bite(
    bite_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    saved = db.query(SavedBite).filter(
        SavedBite.bite_id == bite_id,
        SavedBite.user_id == current_user.id,
    ).first()
    if not saved:
        raise HTTPException(status_code=404, detail="Saved bite not found.")

    db.delete(saved)
    db.commit()
    return {"message": "Removed from saved"}


@router.get("/saved", response_model=list[SavedBiteResponse])
def get_saved_bites(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    saved = (
        db.query(SavedBite)
        .options(joinedload(SavedBite.bite))  # one query, not one per saved bite
        .filter(SavedBite.user_id == current_user.id)
        .order_by(SavedBite.saved_at.desc())
        .all()
    )
    # Task 2 remediation: a saved bite is still source-derived content — a
    # locked source's saves must not surface any more than its Review cards
    # or history rows do. The row itself is preserved (not deleted) so it
    # comes back if the source unlocks again.
    unlocked_bites = {
        b.id for b in _filter_locked_sources(db, current_user, [s.bite for s in saved if s.bite])
    }
    saved = [s for s in saved if s.bite and s.bite.id in unlocked_bites]

    saved_ids = {s.bite_id for s in saved}
    return [
        SavedBiteResponse(
            id=s.id,
            bite=_bite_to_response(s.bite, saved_ids),
            saved_at=s.saved_at,
        )
        for s in saved
    ]
