from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime, date, timedelta
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.streak import Streak
from app.models.push_token import PushToken
from app.schemas.bite import StreakResponse
import uuid

router = APIRouter(prefix="/streak", tags=["streak"])


# ── Cycle math (see NIBBLE_SESSION_LIFECYCLE.md) ──────────────────────────────
# A "cycle" runs from one generation boundary (delivery time − 5 min) to the
# next. The streak survives as long as every cycle contains ≥1 completed
# session; a full cycle with zero completions breaks it. Reading a HELD nibble
# inside its still-open window (e.g. 10:15 for an 11:00 delivery) must count as
# a continuation — which is why these checks anchor on the user's delivery time
# instead of plain calendar days whenever a push token exists.

def _cycle_anchor(db: Session, user_id: str):
    """The user's generation boundary (delivery − 5 min) as UTC (hour, minute),
    or None when no push token / delivery time is registered."""
    row = db.query(PushToken).filter(PushToken.user_id == user_id).first()
    if not row:
        return None
    total = (row.notification_hour * 60 + row.notification_minute - 5) % (24 * 60)
    return total // 60, total % 60


def _streak_is_broken(streak: Streak, db: Session, user_id: str) -> bool:
    """True when a full cycle has passed since the last completed session."""
    if not streak or not streak.current_streak or not streak.last_active_date:
        return False
    now = datetime.utcnow()
    anchor = _cycle_anchor(db, user_id)
    if anchor:
        h, m = anchor
        boundary = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if boundary > now:
            boundary -= timedelta(days=1)
        # The cycle that closed at `boundary` started 24h earlier; no completion
        # on/after that start means the streak died at the boundary. Prefer the
        # exact timestamp — the date alone can't tell a 10:15 read (inside the
        # window) from an 11:30 one (after it closed).
        if streak.last_completed_at:
            return streak.last_completed_at < boundary - timedelta(hours=24)
        return streak.last_active_date < (boundary - timedelta(hours=24)).date()
    # No delivery time known — calendar-day approximation.
    return streak.last_active_date < (now.date() - timedelta(days=1))


def _apply_lazy_reset(streak: Streak, db: Session, user_id: str) -> None:
    """Persist a broken streak as 0 so every reader (app Home, celebration)
    sees the reset immediately, not only after the next check-in."""
    if streak and _streak_is_broken(streak, db, user_id):
        streak.current_streak = 0
        db.commit()
        db.refresh(streak)


@router.get("/", response_model=StreakResponse)
def get_streak(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    streak = db.query(Streak).filter(Streak.user_id == current_user.id).first()
    if not streak:
        return StreakResponse(
            current_streak=0,
            longest_streak=0,
            last_active_date=None,
            total_bites_read=0,
        )
    _apply_lazy_reset(streak, db, current_user.id)
    return streak


@router.post("/checkin", response_model=StreakResponse)
def checkin(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Called when the user COMPLETES a nibble session. Counts once per day.

    Continuation rule: if the streak is still alive at completion time (no
    full cycle has passed without a read — e.g. finishing yesterday's held
    nibble during the final hour before the next delivery), increment.
    Otherwise the streak restarts from 1."""
    today = date.today()
    now = datetime.utcnow()
    streak = db.query(Streak).filter(Streak.user_id == current_user.id).first()

    if not streak:
        streak = Streak(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            current_streak=1,
            longest_streak=1,
            last_active_date=today,
            last_completed_at=now,
            total_bites_read=1,
        )
        db.add(streak)
    elif streak.last_active_date == today:
        streak.total_bites_read += 1   # extra sessions same day still count as reads
        streak.last_completed_at = now
    else:
        if _streak_is_broken(streak, db, current_user.id):
            streak.current_streak = 0
        streak.current_streak = streak.current_streak + 1 if streak.current_streak > 0 else 1
        streak.longest_streak = max(streak.current_streak, streak.longest_streak)
        streak.last_active_date = today
        streak.last_completed_at = now
        streak.total_bites_read += 1

    db.commit()
    db.refresh(streak)
    return streak
