from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import uuid

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.push_token import PushToken

router = APIRouter(prefix="/notifications", tags=["notifications"])


class RegisterTokenRequest(BaseModel):
    token: str
    platform: Optional[str] = None          # 'ios' | 'android'
    notification_hour: Optional[int] = 8    # UTC hour (0-23)
    notification_minute: Optional[int] = 0  # UTC minute — snapped to 5-min steps
    streak_alerts: Optional[bool] = None    # None = leave the stored value alone


class StreakAlertsRequest(BaseModel):
    token: str
    enabled: bool


def _clamp_time(data: RegisterTokenRequest) -> tuple:
    hour = max(0, min(23, data.notification_hour or 8))
    # Scheduler ticks every 5 minutes, so snap to the slot it will check.
    minute = max(0, min(59, data.notification_minute or 0))
    minute = (minute // 5) * 5
    return hour, minute


class RegisterTokenResponse(BaseModel):
    success: bool
    message: str


@router.post("/register", response_model=RegisterTokenResponse)
def register_push_token(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save (or update) a push token for the authenticated user."""
    if not data.token or not data.token.startswith("ExponentPushToken"):
        raise HTTPException(status_code=400, detail="Invalid Expo push token format.")

    notification_hour, notification_minute = _clamp_time(data)

    existing = db.query(PushToken).filter(PushToken.token == data.token).first()
    if existing:
        existing.user_id = current_user.id
        existing.platform = data.platform
        existing.notification_hour = notification_hour
        existing.notification_minute = notification_minute
        if data.streak_alerts is not None:
            existing.streak_alerts_enabled = data.streak_alerts
    else:
        db.add(PushToken(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            token=data.token,
            platform=data.platform,
            notification_hour=notification_hour,
            notification_minute=notification_minute,
            streak_alerts_enabled=True if data.streak_alerts is None else data.streak_alerts,
        ))

    db.commit()
    return {"success": True, "message": "Push token registered."}


@router.delete("/unregister", response_model=RegisterTokenResponse)
def unregister_push_token(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a push token (call on sign-out or when user disables notifications)."""
    deleted = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .delete()
    )
    db.commit()
    if deleted:
        return {"success": True, "message": "Push token removed."}
    return {"success": False, "message": "Token not found."}


@router.put("/streak-alerts", response_model=RegisterTokenResponse)
def update_streak_alerts(
    data: StreakAlertsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle the T−65 'streak ends in 1 hour' push for the user's token(s)."""
    rows = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Token not found. Register first.")
    for row in rows:
        row.streak_alerts_enabled = data.enabled
    db.commit()
    return {"success": True, "message": "Streak alerts updated."}


@router.put("/time", response_model=RegisterTokenResponse)
def update_notification_time(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the preferred delivery time (UTC hour + minute) for an existing token."""
    notification_hour, notification_minute = _clamp_time(data)
    rows = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Token not found. Register first.")
    for row in rows:
        row.notification_hour = notification_hour
        row.notification_minute = notification_minute
    db.commit()
    return {"success": True, "message": "Notification time updated."}
