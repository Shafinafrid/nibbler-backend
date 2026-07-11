from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date, timedelta
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.streak import Streak
from app.schemas.bite import StreakResponse
import uuid

router = APIRouter(prefix="/streak", tags=["streak"])


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
    return streak


@router.post("/checkin", response_model=StreakResponse)
def checkin(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Called when the user COMPLETES a nibble session. Counts once per day."""
    today = date.today()
    streak = db.query(Streak).filter(Streak.user_id == current_user.id).first()

    if not streak:
        streak = Streak(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            current_streak=1,
            longest_streak=1,
            last_active_date=today,
            total_bites_read=1,
        )
        db.add(streak)
    elif streak.last_active_date == today:
        streak.total_bites_read += 1   # extra sessions same day still count as reads
    else:
        yesterday = today - timedelta(days=1)
        streak.current_streak = streak.current_streak + 1 if streak.last_active_date == yesterday else 1
        streak.longest_streak = max(streak.current_streak, streak.longest_streak)
        streak.last_active_date = today
        streak.total_bites_read += 1

    db.commit()
    db.refresh(streak)
    return streak
