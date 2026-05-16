from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, datetime
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.bite import DailyBite, SavedBite
from app.models.streak import Streak
from app.schemas.bite import BiteResponse, SavedBiteResponse, BiteHistoryResponse, StreakResponse
from app.services.bite_generator import BiteGenerator
import uuid

router = APIRouter(prefix="/bites", tags=["bites"])


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


@router.get("/today", response_model=BiteResponse)
async def get_todays_bite(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get today's daily bite. Generates one if it doesn't exist yet."""
    today = date.today()

    bite = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.date == today,
    ).first()

    if not bite:
        if not current_user.profile:
            raise HTTPException(status_code=400, detail="Complete onboarding before getting your daily bite.")

        generator = BiteGenerator(is_premium=current_user.is_premium)
        bite_data = await generator.generate(
            profile=current_user.profile,
            user_id=current_user.id,
        )

        bite = DailyBite(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            date=today,
            **bite_data,
        )
        db.add(bite)

        # Update streak
        background_tasks.add_task(update_streak, current_user.id)
        db.commit()
        db.refresh(bite)

    saved_ids = {s.bite_id for s in db.query(SavedBite).filter(SavedBite.user_id == current_user.id).all()}
    return _bite_to_response(bite, saved_ids)


@router.get("/history", response_model=BiteHistoryResponse)
async def get_bite_history(
    limit: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get past daily bites. Free users: last 7 days. Premium: full archive."""
    query = db.query(DailyBite).filter(DailyBite.user_id == current_user.id)

    if not current_user.is_premium:
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
async def save_bite(
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
    db.commit()
    return {"message": "Saved"}


@router.delete("/{bite_id}/save")
async def unsave_bite(
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
async def get_saved_bites(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    saved = db.query(SavedBite).filter(
        SavedBite.user_id == current_user.id,
    ).order_by(SavedBite.saved_at.desc()).all()

    saved_ids = {s.bite_id for s in saved}
    return [
        SavedBiteResponse(
            id=s.id,
            bite=_bite_to_response(s.bite, saved_ids),
            saved_at=s.saved_at,
        )
        for s in saved
    ]


async def update_streak(user_id: str):
    """Background task to update the user's streak."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        today = date.today()
        streak = db.query(Streak).filter(Streak.user_id == user_id).first()

        if not streak:
            streak = Streak(
                id=str(uuid.uuid4()),
                user_id=user_id,
                current_streak=1,
                longest_streak=1,
                last_active_date=today,
                total_bites_read=1,
            )
            db.add(streak)
        else:
            if streak.last_active_date == today:
                return  # Already checked in today

            from datetime import timedelta
            yesterday = today - timedelta(days=1)
            if streak.last_active_date == yesterday:
                streak.current_streak += 1
            else:
                streak.current_streak = 1

            streak.longest_streak = max(streak.current_streak, streak.longest_streak)
            streak.last_active_date = today
            streak.total_bites_read += 1

        db.commit()
    finally:
        db.close()
