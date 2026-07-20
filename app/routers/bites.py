from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.middleware.auth import get_current_user
from app.rate_limit import limiter
from app.models.user import User
from app.models.bite import DailyBite, SavedBite
from app.models.library import LibraryItem
from app.schemas.bite import BiteResponse, SavedBiteResponse, BiteHistoryResponse
from app.services import mixpanel_service
from app.services.session_service import generate_session_for_item, SessionGenerationError, CARD_TARGETS
from app.config import get_settings
import uuid

router = APIRouter(prefix="/bites", tags=["bites"])
settings = get_settings()

# ── Per-book session generation (July 2026) ───────────────────────────────
# The generation logic lives in app/services/session_service.py (shared with
# the delivery-time scheduler). CARD_TARGETS is imported for read-length checks.


class SessionProfile(BaseModel):
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
    force_new: bool = False
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
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if not item.processed:
        raise HTTPException(status_code=409, detail="Nibbler is still reading this one — try again in a moment.")

    read_length = data.read_length if data.read_length in CARD_TARGETS else 5
    today = _effective_today(data.client_date)

    existing = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.library_item_id == item.id,
        DailyBite.date == today,
    ).first()
    if existing and existing.cards and not data.force_new:
        return _bite_to_session(existing)
    if existing and data.force_new:
        db.delete(existing)
        db.commit()

    # Daily generation caps (free 1 / premium 3, from config — previously
    # defined but never enforced). Re-opening today's existing sessions
    # returns above without counting; force_new regenerates in place because
    # the delete above already freed its slot.
    cap = (
        settings.premium_bites_per_day
        if current_user.effective_premium
        else settings.free_bites_per_day
    )
    todays_generations = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.date == today,
    ).count()
    if todays_generations >= cap:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "daily_limit_reached",
                "message": f"You've used today's {cap} {'bites' if cap > 1 else 'bite'}. Come back tomorrow!",
                "limit": cap,
                "is_premium": current_user.effective_premium,
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
            DailyBite.origin == "scheduled",
            or_(DailyBite.read_at.is_(None), DailyBite.date == today),
        )
        .order_by(DailyBite.date.desc(), DailyBite.generated_at.asc())
        .all()
    )
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
    if bite.read_at is None:
        bite.read_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "read_at": bite.read_at}


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
    saved_ids = {s.bite_id for s in db.query(SavedBite).filter(SavedBite.user_id == current_user.id).all()}

    return BiteHistoryResponse(
        bites=[_bite_to_response(b, saved_ids) for b in bites],
        total=len(bites),
    )


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

    saved_ids = {s.bite_id for s in saved}
    return [
        SavedBiteResponse(
            id=s.id,
            bite=_bite_to_response(s.bite, saved_ids),
            saved_at=s.saved_at,
        )
        for s in saved
    ]
