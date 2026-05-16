from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.streak import Streak
from app.schemas.bite import StreakResponse

router = APIRouter(prefix="/streak", tags=["streak"])


@router.get("/", response_model=StreakResponse)
async def get_streak(
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
